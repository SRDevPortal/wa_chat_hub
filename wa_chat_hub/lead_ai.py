from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe.utils import now_datetime

from wa_chat_hub.prompts import get_conversation_crm_lead
from wa_chat_hub.security import (
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_get_value,
    safe_ai_insert,
    safe_ai_set_value,
)


HOT_TERMS = {
    "rate",
    "rates",
    "price",
    "pricing",
    "courier",
    "shipping",
    "shipment",
    "orders",
    "monthly",
    "cod",
    "prepaid",
    "pickup",
    "delivery",
    "rto",
    "callback",
    "call",
    "shiprocket",
    "delhivery",
}
MEDIUM_TERMS = {
    "d2c",
    "b2c",
    "brand",
    "store",
    "business",
    "aggregator",
    "weight",
    "pincode",
    "zone",
    "surface",
    "express",
    "ndr",
    "tracking",
    "support",
}


def on_chat_message_after_insert(doc, method=None):
    conversation = getattr(doc, "conversation", None)
    if not conversation:
        return

    lead_name = get_conversation_crm_lead(conversation)
    if not lead_name:
        return

    try:
        auto_update_lead_from_conversation(lead_name, conversation=conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA ShipKia Lead AI Auto Update Failed")


def auto_update_lead_from_conversation(lead_name: str, conversation: str | None = None) -> dict[str, Any]:
    if not safe_ai_exists("CRM Lead", lead_name):
        return {"updated": False, "reason": "Lead not found"}

    lead = safe_ai_get_doc("CRM Lead", lead_name)
    context = _get_context_for_lead(lead)
    if context and not context.auto_update_lead_fields:
        return {"updated": False, "reason": "Context auto update disabled"}

    text = _build_lead_text(lead, conversation)
    scoring = _score_text(text)
    extraction = _extract_shipkia_details(text)
    applied = _apply_to_lead(lead, scoring, extraction)

    if applied:
        _create_insight(
            lead=lead,
            conversation=conversation,
            context=context,
            insight_type="ShipKia Scoring",
            output={**scoring, **extraction},
            applied_fields=applied,
            input_snapshot=text[:5000],
        )

    return {"updated": bool(applied), "applied_fields": applied, "score": scoring}


def _get_context_for_lead(lead):
    pipeline = lead.get("sr_lead_pipeline")
    if not pipeline or not safe_ai_exists("DocType", "WA Channel Context"):
        return None

    name = safe_ai_get_value("WA Channel Context", {"pipeline": pipeline, "is_active": 1}, "name")
    return safe_ai_get_doc("WA Channel Context", name) if name else None


def _build_lead_text(lead, conversation: str | None = None) -> str:
    parts = []
    for fieldname in (
        "sr_lead_message",
        "sr_lead_notes",
        "wa_ai_extracted_summary",
        "shipkia_followup_notes",
        "shipkia_shipping_requirement_notes",
    ):
        if lead.get(fieldname):
            parts.append(str(lead.get(fieldname)))

    if conversation:
        rows = safe_ai_get_all(
            "Chat Message",
            filters={"conversation": conversation},
            fields=["body"],
            order_by="creation desc",
            limit_page_length=20,
        )
        parts.extend(row.body for row in rows if row.body)

    return "\n".join(parts)


def _score_text(text: str) -> dict[str, Any]:
    normalized = text.lower()
    hot_hits = sorted(term for term in HOT_TERMS if term in normalized)
    medium_hits = sorted(term for term in MEDIUM_TERMS if term in normalized)

    score = min(100, 30 + len(hot_hits) * 9 + len(medium_hits) * 5)
    if score >= 70:
        band = "Hot"
        next_action = "Sales callback with starting-rate guidance"
    elif score >= 45:
        band = "Medium"
        next_action = "Collect missing shipping details"
    else:
        band = "Low"
        next_action = "Send concise ShipKia follow-up"

    reason_terms = hot_hits[:6] or medium_hits[:6]
    reason = "Matched ShipKia signals: " + ", ".join(reason_terms) if reason_terms else "Limited shipping intent shared yet"
    return {
        "score": score,
        "score_band": band,
        "score_reason": reason,
        "next_action": next_action,
        "confidence": min(95, 45 + len(reason_terms) * 8),
    }


def _extract_shipkia_details(text: str) -> dict[str, Any]:
    summary = _compact_summary(text)
    route = _extract_sentence_matches(text, ["pickup", "delivery", "from", "to", "pincode", "pin code", "zone"])
    rate_context = _extract_sentence_matches(text, ["rate", "price", "pricing", "cod", "prepaid", "rto", "weight"])
    business_context = _extract_sentence_matches(text, ["business", "store", "brand", "d2c", "b2c", "orders", "monthly", "aggregator"])

    return {
        "summary": summary,
        "shipping_route_context": route,
        "rate_context": rate_context,
        "business_context": business_context,
    }


def _apply_to_lead(lead, scoring: dict[str, Any], extraction: dict[str, Any]) -> list[str]:
    meta = frappe.get_meta("CRM Lead")
    updates = {}

    field_map = {
        "wa_ai_score_band": scoring["score_band"],
        "wa_ai_score": scoring["score"],
        "wa_ai_confidence": scoring["confidence"],
        "wa_ai_score_reason": scoring["score_reason"],
        "wa_ai_next_action": scoring["next_action"],
        "wa_ai_last_scored_on": now_datetime(),
        "wa_ai_review_status": "Pending Review",
        "wa_ai_extracted_summary": extraction["summary"],
        "wa_ai_last_extracted_on": now_datetime(),
    }
    for fieldname, value in field_map.items():
        if meta.has_field(fieldname):
            updates[fieldname] = value

    notes = _build_shipkia_note(extraction)
    for notes_field in ("shipkia_followup_notes", "sr_lead_notes"):
        if notes and meta.has_field(notes_field):
            existing_notes = lead.get(notes_field) or ""
            ai_line = f"ShipKia AI Summary: {notes}"
            if ai_line not in existing_notes:
                updates[notes_field] = _append_note(existing_notes, ai_line)
            break

    if not updates:
        return []

    safe_ai_set_value("CRM Lead", lead.name, updates, update_modified=True)
    return sorted(updates)


def _build_shipkia_note(extraction: dict[str, Any]) -> str:
    parts = [
        extraction.get("business_context"),
        extraction.get("shipping_route_context"),
        extraction.get("rate_context"),
    ]
    return " | ".join(str(part).strip() for part in parts if str(part or "").strip()) or extraction.get("summary", "")


def _create_insight(
    lead,
    conversation,
    context,
    insight_type: str,
    output: dict[str, Any],
    applied_fields: list[str],
    input_snapshot: str,
):
    convo = safe_ai_get_doc("Chat Conversation", conversation) if conversation else None
    doc = frappe.get_doc(
        {
            "doctype": "WA Lead AI Insight",
            "lead": lead.name,
            "conversation": conversation,
            "channel_context": context.name if context else None,
            "channel_account": convo.channel_account if convo else None,
            "pipeline": lead.get("sr_lead_pipeline"),
            "insight_type": insight_type,
            "confidence": output.get("confidence"),
            "input_snapshot": input_snapshot,
            "output_json": json.dumps(output, default=str),
            "applied_fields": ", ".join(applied_fields),
        }
    )
    safe_ai_insert(doc)


def _extract_sentence_matches(text: str, keywords: list[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    matches = []
    for sentence in sentences:
        clean = sentence.strip()
        if clean and any(keyword in clean.lower() for keyword in keywords):
            matches.append(clean[:240])
        if len(matches) >= 3:
            break
    return "\n".join(matches)


def _compact_summary(text: str) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    return clean[:500]


def _append_note(existing: str, addition: str) -> str:
    existing = existing.strip()
    if not existing:
        return addition
    return f"{existing}\n\n{addition}"
