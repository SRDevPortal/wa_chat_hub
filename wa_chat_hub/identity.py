from __future__ import annotations

import re
from typing import Any

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from wa_chat_hub.policy import get_conversation_policy


def expire_conversation_verification(
    conversation: str,
    *,
    validity_hours: int | None = None,
    current_time=None,
) -> dict[str, Any]:
    """Patient verification is disabled for the ShipKia customer flow."""
    return {"expired": False, "reason": "patient_flow_disabled"}

    # Legacy patient verification code is intentionally unreachable. It is kept
    # only so older imports/tests do not break while live policy no longer uses it.
    if not conversation or not frappe.db.exists("Chat Conversation", conversation):
        return {"expired": False, "reason": "conversation_not_found"}

    convo = frappe.get_doc("Chat Conversation", conversation)
    policy = get_conversation_policy(convo)
    identity_policy = policy.section("identity_policy") if policy else {}
    if not identity_policy:
        return {"expired": False, "reason": "identity_policy_missing"}
    statuses = identity_policy.get("statuses") or {}
    verified_status = str(statuses.get("verified") or "").strip()
    if not verified_status or getattr(convo, "identity_status", None) != verified_status:
        return {"expired": False, "reason": "not_verified"}
    if validity_hours is None:
        validity_hours = identity_policy.get("validity_hours")
    validity_hours = cint(validity_hours or 0)
    if validity_hours <= 0:
        return {"expired": False, "reason": "expiry_disabled"}

    completed_at = getattr(convo, "verification_completed_at", None)
    now_value = get_datetime(current_time or now_datetime())
    expires_at = (
        add_to_date(get_datetime(completed_at), hours=validity_hours)
        if completed_at
        else None
    )
    if expires_at and now_value < get_datetime(expires_at):
        return {
            "expired": False,
            "reason": "verification_still_valid",
            "expires_at": expires_at,
        }

    linked_patient = getattr(convo, "linked_patient", None) or (
        getattr(convo, "linked_reference_name", None)
        if getattr(convo, "linked_reference_doctype", None) == "Patient"
        else None
    )
    meta = frappe.get_meta("Chat Conversation")
    values = {
        "identity_status": (
            statuses.get("matched") if linked_patient else statuses.get("unverified")
        ),
        "verification_completed_at": None,
        "verification_started_at": None,
        "verification_attempts": 0,
        "routing_reason": "patient_verification_expired",
    }
    values = {
        key: value for key, value in values.items()
        if key != "identity_status" or value not in (None, "")
    }
    values = {key: value for key, value in values.items() if meta.has_field(key)}
    frappe.db.set_value("Chat Conversation", conversation, values, update_modified=False)
    return {
        "expired": True,
        "reason": "verification_expired" if completed_at else "verification_timestamp_missing",
        "patient": linked_patient,
        "expired_at": expires_at,
        "values": values,
    }


def reconcile_conversation_identity(
    conversation: str,
    *,
    patient: str | None = None,
    crm_lead: str | None = None,
    source: str = "runtime",
    verified: bool = False,
) -> dict[str, Any]:
    if not conversation or not frappe.db.exists("Chat Conversation", conversation):
        return {"changed": False, "reason": "conversation_not_found"}

    convo = frappe.get_doc("Chat Conversation", conversation)
    policy = get_conversation_policy(convo)
    if not policy:
        return {"changed": False, "reason": "identity_policy_missing"}
    identity_policy = policy.section("identity_policy")
    routing_policy = policy.section("party_routing_policy")
    statuses = identity_policy.get("statuses") or {}
    party_types = routing_policy.get("party_type_by_reference_doctype") or {}
    verified_status = str(statuses.get("verified") or "").strip()
    matched_status = str(statuses.get("matched") or "").strip()
    unverified_status = str(statuses.get("unverified") or "").strip()
    lead_party_type = str(
        party_types.get("Lead") or party_types.get("Lead") or ""
    ).strip()
    customer_party_type = str(party_types.get("Customer") or "").strip()
    unknown_party_type = str(routing_policy.get("unknown_party_type") or "").strip()
    if not all((matched_status, unverified_status, unknown_party_type)):
        return {"changed": False, "reason": "identity_policy_incomplete"}
    meta = frappe.get_meta("Chat Conversation")
    values: dict[str, Any] = {"last_identity_sync_at": now_datetime()}

    if getattr(convo, "linked_lead", None):
        values.update({"party_type": lead_party_type, "identity_status": matched_status})
    elif getattr(convo, "linked_reference_doctype", None) in ("Lead",):
        values.update({"party_type": lead_party_type, "identity_status": matched_status})
    elif getattr(convo, "linked_reference_doctype", None) == "Customer":
        values.update({"party_type": customer_party_type or "Customer", "identity_status": matched_status})
    else:
        values.update({"party_type": unknown_party_type, "identity_status": unverified_status})

    values = {key: value for key, value in values.items() if meta.has_field(key)}
    changed = any(str(getattr(convo, key, None) or "") != str(value or "") for key, value in values.items())
    if changed:
        frappe.db.set_value("Chat Conversation", conversation, values, update_modified=False)
    return {"changed": changed, "patient": None, "values": values, "source": source}


def reconcile_patient_conversations(doc, method=None) -> None:
    return
    if not doc or not getattr(doc, "name", None):
        return
    conversations = set(
        frappe.get_all("Chat Conversation", filters={"linked_reference_doctype": "Patient", "linked_reference_name": doc.name}, pluck="name")
    )
    if frappe.get_meta("Chat Conversation").has_field("linked_patient"):
        conversations.update(frappe.get_all("Chat Conversation", filters={"linked_patient": doc.name}, pluck="name"))
    for conversation in conversations:
        reconcile_conversation_identity(conversation, patient=doc.name, source="patient_hook")


def reconcile_patient_encounter(doc, method=None) -> None:
    return
    patient = doc.get("patient") if doc else None
    lead = doc.get("sr_source_crm_lead") if doc else None
    if not patient:
        return
    filters = []
    if lead:
        filters.append({"linked_lead": lead})
    if frappe.get_meta("Chat Conversation").has_field("linked_patient"):
        filters.append({"linked_patient": patient})
    filters.append({"linked_reference_doctype": "Patient", "linked_reference_name": patient})
    conversations: set[str] = set()
    for row_filters in filters:
        conversations.update(frappe.get_all("Chat Conversation", filters=row_filters, pluck="name"))
    for conversation in conversations:
        reconcile_conversation_identity(
            conversation,
            patient=patient,
            crm_lead=lead,
            source="patient_encounter",
        )


def verify_patient_identity_from_inbound_message(
    conversation: str,
    message: str,
) -> dict[str, Any]:
    """Patient verification is disabled for the ShipKia customer flow."""
    return {"verified": False, "reason": "patient_flow_disabled"}

    if not conversation or not message:
        return {"verified": False, "reason": "conversation_or_message_missing"}
    if not frappe.db.exists("Chat Conversation", conversation):
        return {"verified": False, "reason": "conversation_not_found"}
    if not frappe.db.exists("Chat Message", message):
        return {"verified": False, "reason": "message_not_found"}

    convo = frappe.get_doc("Chat Conversation", conversation)
    policy = get_conversation_policy(convo)
    identity_policy = policy.section("identity_policy") if policy else {}
    if not identity_policy:
        return {"verified": False, "reason": "identity_policy_missing"}
    verified_status = (identity_policy.get("statuses") or {}).get("verified")
    if verified_status and getattr(convo, "identity_status", None) == verified_status:
        return {
            "verified": True,
            "reason": "already_verified",
            "patient": getattr(convo, "linked_patient", None),
        }

    patient, _ = _trusted_patient(convo, None, getattr(convo, "linked_lead", None))
    if not patient:
        return {"verified": False, "reason": "linked_patient_missing"}

    msg = frappe.get_doc("Chat Message", message)
    if str(msg.conversation) != str(conversation) or msg.direction != "Inbound":
        return {"verified": False, "reason": "message_not_current_inbound"}

    chat_phone = _normalized_phone(
        frappe.db.get_value("Chat Contact", convo.contact, "phone_number")
        if getattr(convo, "contact", None)
        else None,
        identity_policy,
    )
    supplied_phones = _phones_from_text(msg.body, identity_policy)
    if not chat_phone:
        return {"verified": False, "reason": "chat_phone_missing", "patient": patient}
    claims_current_number = _claims_current_chat_number(msg.body, identity_policy)
    if supplied_phones and chat_phone not in supplied_phones:
        return {"verified": False, "reason": "supplied_phone_mismatch", "patient": patient}
    if not supplied_phones and not claims_current_number:
        return {
            "verified": False,
            "reason": "current_number_ownership_not_confirmed",
            "patient": patient,
        }

    patient_phone_field = _matching_patient_phone_field(patient, chat_phone, identity_policy)
    if not patient_phone_field:
        return {"verified": False, "reason": "patient_phone_mismatch", "patient": patient}

    identity = reconcile_conversation_identity(
        conversation,
        patient=patient,
        source="patient_phone_match",
        verified=True,
    )
    from wa_chat_hub.agent_router import persist_agent_route, resolve_agent_route

    route = resolve_agent_route(conversation)
    persist_agent_route(conversation, route)
    return {
        "verified": True,
        "reason": (
            "current_chat_number_ownership_match"
            if claims_current_number and not supplied_phones
            else "three_way_phone_match"
        ),
        "patient": patient,
        "patient_phone_field": patient_phone_field,
        "identity": identity,
        "agent_profile": route.agent_profile,
    }


def verify_patient_identity_by_agent(
    *,
    patient: str,
    conversation: str,
) -> dict[str, Any]:
    """Patient verification is disabled for the ShipKia customer flow."""
    return {"verified": False, "reason": "patient_flow_disabled"}

    if not conversation or not frappe.db.exists("Chat Conversation", conversation):
        frappe.throw("Chat Conversation was not found.", frappe.PermissionError)

    convo = frappe.get_doc("Chat Conversation", conversation)
    linked_patient, _source = _trusted_patient(
        convo,
        None,
        getattr(convo, "linked_lead", None),
    )
    if not linked_patient or linked_patient != patient:
        frappe.throw(
            "The patient does not match the conversation identity.",
            frappe.PermissionError,
        )

    latest_message = frappe.get_all(
        "Chat Message",
        filters={"conversation": conversation, "direction": "Inbound"},
        pluck="name",
        order_by="creation desc",
        limit_page_length=1,
    )
    if not latest_message:
        return {"verified": False, "reason": "inbound_message_missing"}
    return verify_patient_identity_from_inbound_message(
        conversation,
        latest_message[0],
    )


def _phones_from_text(text: str | None, identity_policy: dict[str, Any]) -> set[str]:
    if not identity_policy.get("allow_supplied_registered_number"):
        return set()
    minimum_digits = max(1, cint(identity_policy.get("phone_candidate_min_digits") or 0))
    phones: set[str] = set()
    for candidate in re.findall(r"\+?[\d][\d\s().-]*", str(text or "")):
        if sum(1 for char in candidate if char.isdigit()) < minimum_digits:
            continue
        normalized = _normalized_phone(candidate, identity_policy)
        if normalized:
            phones.add(normalized)
    return phones


def _claims_current_chat_number(text: str | None, identity_policy: dict[str, Any]) -> bool:
    """Match only administrator-configured ownership phrases."""
    if not identity_policy.get("allow_current_chat_number_claim"):
        return False
    normalized = _normalized_phrase(text)
    configured = identity_policy.get("current_number_claim_phrases") or {}
    phrase_groups = configured.values() if isinstance(configured, dict) else [configured]
    return any(
        normalized == _normalized_phrase(phrase)
        for phrases in phrase_groups
        for phrase in (phrases or [])
        if normalized
    )


def _normalized_phrase(value: str | None) -> str:
    return " ".join(re.findall(r"[\w]+", str(value or "").casefold(), re.UNICODE))


def _normalized_phone(
    value: str | None,
    identity_policy: dict[str, Any],
) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    match_digits = cint(identity_policy.get("phone_match_last_digits") or 0)
    if match_digits <= 0 or len(digits) < match_digits:
        return None
    return digits[-match_digits:]


def _matching_patient_phone_field(
    patient: str,
    phone: str,
    identity_policy: dict[str, Any],
) -> str | None:
    meta = frappe.get_meta("Patient")
    configured = (identity_policy.get("phone_fields") or {}).get("Patient") or []
    fields = [str(field) for field in configured if meta.has_field(str(field))]
    if not fields:
        return None
    values = frappe.db.get_value("Patient", patient, fields, as_dict=True) or {}
    for field in fields:
        if _normalized_phone(values.get(field), identity_policy) == phone:
            return field
    return None


def _trusted_patient(convo, patient: str | None, crm_lead: str | None) -> tuple[str | None, str | None]:
    return None, None
    if patient and frappe.db.exists("Patient", patient):
        return patient, "explicit"
    linked_patient = getattr(convo, "linked_patient", None)
    if linked_patient and frappe.db.exists("Patient", linked_patient):
        return linked_patient, "conversation"
    if getattr(convo, "linked_reference_doctype", None) == "Patient":
        reference = getattr(convo, "linked_reference_name", None)
        if reference and frappe.db.exists("Patient", reference):
            return reference, "reference"
    if getattr(convo, "contact", None):
        contact_patient = frappe.db.get_value("Chat Contact", convo.contact, "linked_patient")
        if contact_patient and frappe.db.exists("Patient", contact_patient):
            return contact_patient, "contact"
    lead = crm_lead or getattr(convo, "linked_lead", None)
    if lead and frappe.db.exists("Lead", lead) and frappe.get_meta("Lead").has_field("sr_source_patient"):
        source_patient = frappe.db.get_value("Lead", lead, "sr_source_patient")
        if source_patient and frappe.db.exists("Patient", source_patient):
            return source_patient, "crm_lead_conversion"
    return None, None
