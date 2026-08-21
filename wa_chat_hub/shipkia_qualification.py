from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import frappe
from frappe.utils import add_to_date, cint, flt, get_datetime, now_datetime
from frappe.utils.background_jobs import enqueue

from wa_chat_hub.security import safe_ai_exists, safe_ai_get_all, safe_ai_get_doc, safe_ai_get_value, safe_ai_set_value
from wa_chat_hub.shipkia_aggregators import normalize_shipkia_aggregator
from wa_chat_hub.services import normalize_phone


QUALIFICATION_DEBOUNCE_SECONDS = 60
QUALIFICATION_SWEEP_LIMIT = 50
SHIPKIA_LEAD_DOCTYPE = "Lead"


def capture_and_schedule_qualification(conversation: str, message_name: str | None = None) -> dict[str, Any]:
    """Cheap per-message capture; full qualification runs later from scheduler."""
    lead_name = _lead_for_conversation(conversation)
    if not lead_name:
        return {"scheduled": False, "reason": "lead_not_found"}

    message = _load_message(message_name) if message_name else None
    text = str((message or {}).get("body") or "").strip()
    updates = _extract_obvious_lead_updates(text)
    updates.update(_ai_tracking_updates(lead_name, text))
    _set_if_fields_exist(lead_name, updates)
    _enqueue_pending_qualification_sweep()

    return {"scheduled": True, "lead": lead_name}


def _enqueue_pending_qualification_sweep() -> None:
    try:
        enqueue(
            "wa_chat_hub.shipkia_qualification.process_pending_qualifications",
            queue="short",
            limit=QUALIFICATION_SWEEP_LIMIT,
            timeout=120,
            enqueue_after_commit=True,
            job_id="shipkia_pending_qualification_sweep",
            deduplicate=True,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "ShipKia Qualification Sweep Enqueue Failed")


def process_pending_qualifications(limit: int = QUALIFICATION_SWEEP_LIMIT) -> dict[str, Any]:
    """Scheduler sweep for leads whose debounce window has passed."""
    if not safe_ai_exists("DocType", SHIPKIA_LEAD_DOCTYPE):
        return {"processed": 0, "skipped": 0}

    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    if not meta.has_field("shipkia_ai_pending_qualification_at"):
        return {"processed": 0, "skipped": 0}

    rows = safe_ai_get_all(
        SHIPKIA_LEAD_DOCTYPE,
        filters={
            "shipkia_ai_pending_qualification_at": ["<=", now_datetime()],
        },
        fields=["name"],
        order_by="shipkia_ai_pending_qualification_at asc",
        limit_page_length=cint(limit) or QUALIFICATION_SWEEP_LIMIT,
    )

    processed = 0
    skipped = 0
    for row in rows:
        result = qualify_lead(row.name)
        if result.get("updated"):
            processed += 1
        else:
            skipped += 1
    return {"processed": processed, "skipped": skipped}


def qualify_lead(lead_name: str) -> dict[str, Any]:
    if not lead_name or not safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, lead_name):
        return {"updated": False, "reason": "lead_not_found"}

    lead = safe_ai_get_doc(SHIPKIA_LEAD_DOCTYPE, lead_name)
    conversation = _conversation_for_lead(lead_name)
    messages = _recent_messages(conversation)
    lead_context = _lead_context(lead)
    scorecard = _score_lead(lead_context, messages)
    fingerprint = _qualification_fingerprint(lead_context, messages)

    existing_fingerprint = getattr(lead, "shipkia_ai_qualification_fingerprint", None)
    if existing_fingerprint == fingerprint and getattr(lead, "shipkia_ai_pending_qualification_at", None):
        _set_if_fields_exist(lead_name, {"shipkia_ai_pending_qualification_at": None})
        return {"updated": False, "reason": "unchanged"}

    updates = {
        "shipkia_ai_qualified": 1 if scorecard["status"] == "Qualified" else 0,
        "shipkia_ai_lead_temperature": scorecard["temperature"],
        "shipkia_ai_qualification_status": scorecard["status"],
        "shipkia_ai_qualification_score": scorecard["score"],
        "shipkia_ai_context_complete": 1 if scorecard["context_complete"] else 0,
        "shipkia_ai_onboarding_assisted": 1 if scorecard["onboarding_assisted"] else 0,
        "shipkia_ai_onboarding_stage": scorecard["onboarding_stage"],
        "shipkia_ai_last_action": scorecard["last_action"],
        "shipkia_ai_last_qualified_at": now_datetime(),
        "shipkia_ai_messages_count": scorecard["inbound_count"],
        "shipkia_ai_details_collected": scorecard["details_collected"],
        "shipkia_ai_pending_qualification_at": None,
        "shipkia_ai_qualification_fingerprint": fingerprint,
        "shipkia_ai_qualification_reason": scorecard["reason"],
    }
    if scorecard.get("frustration_detected"):
        if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_callback_required"):
            updates["shipkia_callback_required"] = 1
        if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_objections"):
            existing_objections = getattr(lead, "shipkia_objections", None) or ""
            note = "Customer frustration: bot repeated questions or did not understand context."
            updates["shipkia_objections"] = existing_objections if note in existing_objections else f"{existing_objections}\n{note}".strip()
        if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_followup_notes"):
            existing_notes = getattr(lead, "shipkia_followup_notes", None) or ""
            note = "Human follow-up needed: customer showed frustration after qualification flow."
            updates["shipkia_followup_notes"] = existing_notes if note in existing_notes else f"{existing_notes}\n{note}".strip()
    if scorecard.get("rate_shared") and frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_rate_shared"):
        updates["shipkia_rate_shared"] = 1

    if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_lead_temperature"):
        updates["shipkia_lead_temperature"] = scorecard["temperature"]
    if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_context_completed"):
        updates["shipkia_context_completed"] = 1 if scorecard["context_complete"] else 0
    if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_sales_stage"):
        updates["shipkia_sales_stage"] = scorecard["sales_stage"]
    aggregator_name = str(lead_context.get("shipkia_current_aggregator_name") or "").strip()
    if aggregator_name:
        if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_current_aggregator_status"):
            updates["shipkia_current_aggregator_status"] = "Yes"
        verified_name = normalize_shipkia_aggregator(aggregator_name)
        if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_current_aggregator_verified"):
            updates["shipkia_current_aggregator_verified"] = 1 if verified_name else 0
        if verified_name and frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_current_aggregator_name"):
            updates["shipkia_current_aggregator_name"] = verified_name
        if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field("shipkia_current_aggregator_raw"):
            updates["shipkia_current_aggregator_raw"] = "" if verified_name else aggregator_name

    _set_if_fields_exist(lead_name, updates)
    return {"updated": True, "lead": lead_name, **scorecard}


def _load_message(message_name: str | None) -> dict[str, Any]:
    if not message_name or not safe_ai_exists("Chat Message", message_name):
        return {}
    return safe_ai_get_value(
        "Chat Message",
        message_name,
        ["name", "body", "content_type", "direction", "creation"],
        as_dict=True,
    ) or {}


def _lead_for_conversation(conversation: str | None) -> str:
    if not conversation or not safe_ai_exists("Chat Conversation", conversation):
        return ""

    convo = safe_ai_get_value(
        "Chat Conversation",
        conversation,
        ["linked_reference_doctype", "linked_reference_name", "contact"],
        as_dict=True,
    ) or {}
    if convo.get("linked_reference_doctype") == SHIPKIA_LEAD_DOCTYPE and convo.get("linked_reference_name"):
        name = str(convo.get("linked_reference_name"))
        if safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, name):
            return name

    contact_name = convo.get("contact")
    if contact_name and safe_ai_exists("Chat Contact", contact_name):
        contact = safe_ai_get_value(
            "Chat Contact",
            contact_name,
            ["linked_lead", "source_doctype", "source_name", "phone_number"],
            as_dict=True,
        ) or {}
        for candidate in (contact.get("linked_lead"), contact.get("source_name")):
            if candidate and safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, candidate):
                return str(candidate)
        if contact.get("phone_number"):
            return _find_lead_by_phone(str(contact.get("phone_number") or ""))
    return ""


def _find_lead_by_phone(phone: str) -> str:
    normalized = normalize_phone(phone)
    if not normalized:
        return ""
    last10 = normalized[-10:] if len(normalized) >= 10 else normalized
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    for fieldname in ("mobile_no", "phone", "whatsapp_no", "custom_whatsapp_number"):
        if not meta.has_field(fieldname):
            continue
        exact = safe_ai_get_value(SHIPKIA_LEAD_DOCTYPE, {fieldname: normalized}, "name")
        if exact:
            return str(exact)
        rows = safe_ai_get_all(
            SHIPKIA_LEAD_DOCTYPE,
            filters={fieldname: ["like", f"%{last10}%"]},
            fields=["name", fieldname],
            limit_page_length=10,
        )
        for row in rows:
            value = normalize_phone(row.get(fieldname))
            if value and (value == normalized or value.endswith(last10)):
                return str(row.name)
    return ""


def _conversation_for_lead(lead_name: str) -> str:
    if not lead_name or not safe_ai_exists("DocType", "Chat Conversation"):
        return ""
    return (
        safe_ai_get_value(
            "Chat Conversation",
            {"linked_reference_doctype": SHIPKIA_LEAD_DOCTYPE, "linked_reference_name": lead_name},
            "name",
        )
        or ""
    )


def _recent_messages(conversation: str, limit: int = 40) -> list[dict[str, Any]]:
    if not conversation:
        return []
    rows = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["direction", "sender_type", "body", "content_type", "creation"],
        order_by="creation desc",
        limit_page_length=limit,
    )
    return list(reversed(rows or []))


def _lead_context(lead) -> dict[str, Any]:
    fields = [
        "shipkia_business_type",
        "shipkia_business_name",
        "shipkia_current_aggregator_status",
        "shipkia_current_aggregator_name",
        "shipkia_current_shipping_rate",
        "shipkia_rto_percentage",
        "shipkia_monthly_shipments",
        "shipkia_context_completed",
        "shipkia_callback_required",
        "shipkia_signup_link_sent",
        "shipkia_account_created",
        "shipkia_first_shipment_done",
    ]
    return {field: lead.get(field) for field in fields if frappe.get_meta(SHIPKIA_LEAD_DOCTYPE).has_field(field)}


def _extract_obvious_lead_updates(text: str) -> dict[str, Any]:
    lower = (text or "").lower()
    updates: dict[str, Any] = {}

    business_type = _parse_business_type(lower)
    if business_type:
        updates["shipkia_business_type"] = business_type

    business_name = _parse_business_name(text)
    if business_name:
        updates["shipkia_business_name"] = business_name

    monthly_shipments = _parse_monthly_shipments(lower)
    if monthly_shipments is not None:
        updates["shipkia_monthly_shipments"] = monthly_shipments

    aggregator_name = _parse_aggregator_name(text)
    if aggregator_name:
        verified_name = normalize_shipkia_aggregator(aggregator_name)
        updates["shipkia_current_aggregator_status"] = "Yes"
        updates["shipkia_current_aggregator_name"] = verified_name or aggregator_name
        updates["shipkia_current_aggregator_verified"] = 1 if verified_name else 0
        updates["shipkia_current_aggregator_raw"] = "" if verified_name else aggregator_name
    elif re.search(r"\b(no|not using|without aggregator|directly|khud|nahi|nahin)\b", lower) and "aggregator" in lower:
        updates["shipkia_current_aggregator_status"] = "No"

    current_rate = _parse_money_value(lower)
    if current_rate and re.search(r"\b(current|abhi|currently|mil|getting|rate|charges?)\b", lower):
        updates["shipkia_current_shipping_rate"] = current_rate

    rto = _parse_rto(lower)
    if rto is not None:
        updates["shipkia_rto_percentage"] = rto

    if re.search(r"\b(callback|call back|call kara|call arrange|exact rate|final rate|proper rate)\b", lower):
        updates["shipkia_callback_required"] = 1
    if _has_frustration_intent(lower):
        updates["shipkia_callback_required"] = 1
        updates["shipkia_ai_qualification_status"] = "Needs Human Review"
        updates["shipkia_ai_lead_temperature"] = "Hot"
        updates["shipkia_lead_temperature"] = "Hot"
        updates["shipkia_objections"] = "Customer frustration: bot repeated questions or did not understand context."

    return {key: value for key, value in updates.items() if value not in (None, "")}


def _ai_tracking_updates(lead_name: str, text: str) -> dict[str, Any]:
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    updates: dict[str, Any] = {
        "shipkia_ai_last_message_at": now_datetime(),
        "shipkia_ai_pending_qualification_at": add_to_date(
            now_datetime(),
            seconds=QUALIFICATION_DEBOUNCE_SECONDS,
            as_datetime=True,
        ),
    }
    if meta.has_field("shipkia_ai_messages_count"):
        current = cint(safe_ai_get_value(SHIPKIA_LEAD_DOCTYPE, lead_name, "shipkia_ai_messages_count") or 0)
        updates["shipkia_ai_messages_count"] = current + 1
    last_action = _last_action_from_text(text)
    if last_action:
        updates["shipkia_ai_last_action"] = last_action
    return updates


def _score_lead(context: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    text = "\n".join(str(row.get("body") or "") for row in messages if row.get("direction") == "Inbound")
    lower = text.lower()
    inbound_count = sum(1 for row in messages if row.get("direction") == "Inbound" and str(row.get("body") or "").strip())
    if context.get("shipkia_current_aggregator_name") and context.get("shipkia_current_aggregator_status") != "Yes":
        context["shipkia_current_aggregator_status"] = "Yes"
    details = _details_collected(context)

    score = 10 + min(12, inbound_count * 2) + details * 6
    reasons = []

    monthly = cint(context.get("shipkia_monthly_shipments") or 0)
    if monthly >= 1000:
        score += 25
        reasons.append(f"{monthly} monthly shipments")
    elif monthly >= 300:
        score += 18
        reasons.append(f"{monthly} monthly shipments")
    elif monthly > 0:
        score += 8
        reasons.append(f"{monthly} monthly shipments")

    if context.get("shipkia_business_type"):
        reasons.append(f"{context.get('shipkia_business_type')} business")
    if context.get("shipkia_current_aggregator_name"):
        score += 8
        reasons.append(f"using {context.get('shipkia_current_aggregator_name')}")
    elif context.get("shipkia_current_aggregator_status"):
        score += 4
        reasons.append(f"aggregator status {context.get('shipkia_current_aggregator_status')}")
    if flt(context.get("shipkia_current_shipping_rate")) > 0:
        score += 6
        reasons.append("current rate shared")
    if flt(context.get("shipkia_rto_percentage")) > 0:
        score += 6
        reasons.append("RTO shared")

    if _has_rate_intent(lower):
        score += 10
        reasons.append("asked rates")
    if _has_callback_intent(lower) or cint(context.get("shipkia_callback_required")):
        score += 18
        reasons.append("callback requested")
    if _has_onboarding_intent(lower):
        score += 20
        reasons.append("onboarding intent")
    frustration_detected = _has_frustration_intent(lower)
    if frustration_detected:
        score += 15
        reasons.append("customer frustration needs human recovery")

    context_complete = _context_complete(context)
    if context_complete:
        score += 10
        reasons.append("required context complete")

    score = min(100, max(0, int(score)))
    temperature = "Hot" if score >= 70 else "Warm" if score >= 40 else "Cold"
    status = _qualification_status(score, context_complete, inbound_count)
    if frustration_detected:
        temperature = "Hot"
        status = "Needs Human Review"
    onboarding_assisted = _has_onboarding_intent(lower) or _onboarding_stage(context) != "Not Started"
    onboarding_stage = _onboarding_stage(context)
    if onboarding_stage == "Not Started" and context_complete:
        onboarding_stage = "Details Collected"

    return {
        "score": score,
        "temperature": temperature,
        "status": status,
        "context_complete": context_complete,
        "details_collected": details,
        "inbound_count": inbound_count,
        "onboarding_assisted": onboarding_assisted,
        "onboarding_stage": onboarding_stage,
        "sales_stage": "Qualified" if status == "Qualified" else "Nurturing" if temperature in {"Warm", "Hot"} else "New",
        "last_action": _last_action_from_text(text) or "Qualified lead",
        "reason": "; ".join(reasons[:6]) or "Limited ShipKia qualification signals yet",
        "frustration_detected": frustration_detected,
        "rate_shared": _has_outbound_rate_shared(messages),
    }


def _qualification_status(score: int, context_complete: bool, inbound_count: int) -> str:
    if context_complete and score >= 55:
        return "Qualified"
    if score >= 40:
        return "In Progress"
    if inbound_count >= 5:
        return "Needs Human Review"
    return "New"


def _details_collected(context: dict[str, Any]) -> int:
    keys = (
        "shipkia_business_type",
        "shipkia_business_name",
        "shipkia_current_aggregator_status",
        "shipkia_current_aggregator_name",
        "shipkia_current_shipping_rate",
        "shipkia_rto_percentage",
        "shipkia_monthly_shipments",
    )
    return sum(1 for key in keys if context.get(key) not in (None, "", 0, "0"))


def _context_complete(context: dict[str, Any]) -> bool:
    if not context.get("shipkia_business_type"):
        return False
    if not context.get("shipkia_business_name"):
        return False
    if not context.get("shipkia_current_aggregator_status"):
        return False
    if context.get("shipkia_current_aggregator_status") == "Yes" and not context.get("shipkia_current_aggregator_name"):
        return False
    if context.get("shipkia_current_aggregator_status") == "Yes":
        if flt(context.get("shipkia_current_shipping_rate")) <= 0:
            return False
        if flt(context.get("shipkia_rto_percentage")) <= 0:
            return False
    return cint(context.get("shipkia_monthly_shipments") or 0) > 0


def _onboarding_stage(context: dict[str, Any]) -> str:
    if cint(context.get("shipkia_first_shipment_done")):
        return "First Shipment Done"
    if cint(context.get("shipkia_account_created")):
        return "Account Created"
    if cint(context.get("shipkia_signup_link_sent")):
        return "Signup Link Sent"
    return "Not Started"


def _qualification_fingerprint(context: dict[str, Any], messages: list[dict[str, Any]]) -> str:
    payload = {
        "context": context,
        "messages": [
            {
                "direction": row.get("direction"),
                "body": str(row.get("body") or "")[:500],
                "creation": str(row.get("creation") or ""),
            }
            for row in messages[-20:]
        ],
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _set_if_fields_exist(lead_name: str, updates: dict[str, Any]) -> None:
    if not updates:
        return
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    filtered = {key: value for key, value in updates.items() if meta.has_field(key)}
    if filtered:
        safe_ai_set_value(SHIPKIA_LEAD_DOCTYPE, lead_name, filtered, update_modified=True)


def _parse_business_type(lower: str) -> str:
    if re.search(r"\bd2c\b|direct\s*to\s*consumer", lower):
        return "D2C"
    if re.search(r"\bb2c\b|business\s*to\s*consumer", lower):
        return "B2C"
    return ""


def _parse_business_name(text: str) -> str:
    value = str(text or "").strip()
    patterns = (
        r"\b(?:business|store|brand|company|shop)\s*(?:name\s*)?(?:is|hai|:|-)?\s+(.+)$",
        r"\bmy\s+(?:business|store|brand|company|shop)\s+(?:is|name is|called)?\s*(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        name = re.split(
            r"\b(?:and|with|using|currently|monthly|orders?|shipments?|per month|aggregator|rto|rate|rates)\b|,",
            match.group(1).strip(),
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .:-")
        if 2 <= len(name) <= 80:
            return name
    return ""


def _parse_monthly_shipments(lower: str) -> int | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(k)?\s*(?:orders?|shipments?|parcels?)\s*(?:/|per)?\s*(?:month|monthly|mahine)?", lower)
    if not match:
        match = re.search(r"(?:monthly|month|mahine).*?(\d+(?:\.\d+)?)\s*(k)?", lower)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2):
        value *= 1000
    return int(value)


def _parse_aggregator_name(text: str) -> str:
    lower = (text or "").lower()
    known = normalize_shipkia_aggregator(text or "")
    if known:
        return known
    match = re.search(
        r"\b(?:using|use|through|with|currently using|aggregator(?: is)?|partner(?: is)?)\s+([a-zA-Z0-9][a-zA-Z0-9 &._-]{1,40})",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    name = re.split(r"\b(?:right now|abhi|and|rate|rates|rto|orders?|shipments?|monthly|per month)\b|,", match.group(1), maxsplit=1, flags=re.IGNORECASE)[0]
    name = name.strip(" .:-")[:80]
    known = normalize_shipkia_aggregator(name)
    return known or name


def _parse_money_value(lower: str) -> float | None:
    match = re.search(r"(?:rs\.?|inr|₹)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:rs\.?|rupees|inr)", lower)
    if not match:
        return None
    return flt(match.group(1) or match.group(2))


def _parse_rto(lower: str) -> float | None:
    if "rto" not in lower and "return" not in lower:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", lower)
    if match:
        return flt(match.group(1))
    match = re.search(r"(?:rto|return)[^\d]{0,20}(\d+(?:\.\d+)?)", lower)
    return flt(match.group(1)) if match else None


def _has_rate_intent(lower: str) -> bool:
    return bool(re.search(r"\b(rate|rates|price|pricing|charges?|shipping cost|courier cost)\b", lower))


def _has_callback_intent(lower: str) -> bool:
    return bool(re.search(r"\b(callback|call back|call kara|call arrange|exact rate|final rate|proper rate)\b", lower))


def _has_frustration_intent(lower: str) -> bool:
    return bool(
        re.search(
            r"\b(repeat|dubara|dobara|baar baar|bar bar|samjh|samajh|understand|not reading|mujhe nh krna|nahi karna|frustrat|confus)",
            lower,
        )
    )


def _has_outbound_rate_shared(messages: list[dict[str, Any]]) -> bool:
    for row in messages:
        if row.get("direction") != "Outbound":
            continue
        body = str(row.get("body") or "").lower()
        if (
            ("rate" in body or "rates" in body)
            and ("start" in body or "se start" in body)
            and re.search(r"(₹|rs\.?|inr)\s*\d|\d+(?:\.\d+)?\s*(?:rs|inr|rupees)", body)
        ):
            return True
    return False


def _has_onboarding_intent(lower: str) -> bool:
    return bool(re.search(r"\b(onboard|onboarding|signup|sign up|register|account create|shipkia panel|start shipping|start kar)\b", lower))


def _last_action_from_text(text: str) -> str:
    lower = (text or "").lower()
    if _has_onboarding_intent(lower):
        return "Onboarding intent detected"
    if _has_callback_intent(lower):
        return "Callback requested"
    if _has_frustration_intent(lower):
        return "Human review needed"
    if _has_rate_intent(lower):
        return "Rate query handled"
    if _parse_monthly_shipments(lower) is not None:
        return "Monthly shipments captured"
    if _parse_aggregator_name(text):
        return "Aggregator captured"
    if _parse_business_type(lower):
        return "Business type captured"
    return "Lead context updated"
