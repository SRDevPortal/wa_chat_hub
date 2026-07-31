from __future__ import annotations

import re
from typing import Any

import frappe
from frappe.utils import now_datetime


PATIENT_PHONE_FIELDS = ("mobile", "mobile_no", "phone", "custom_whatsapp_number")
VERIFICATION_PATIENT_PHONE_FIELDS = PATIENT_PHONE_FIELDS
PHONE_CANDIDATE_PATTERN = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{8,}\d)(?!\d)")


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
    meta = frappe.get_meta("Chat Conversation")
    patient, patient_source = _trusted_patient(convo, patient, crm_lead)
    values: dict[str, Any] = {"last_identity_sync_at": now_datetime()}

    if patient and frappe.db.exists("Patient", patient):
        identity_status = (
            "Verified"
            if verified or getattr(convo, "identity_status", None) == "Verified"
            else "Matched"
        )
        values.update(
            {
                "linked_patient": patient,
                "party_type": "Patient",
                "identity_status": identity_status,
                "linked_reference_doctype": "Patient",
                "linked_reference_name": patient,
                "routing_reason": f"patient_identity:{patient_source or source}",
            }
        )
        if verified and meta.has_field("verification_completed_at"):
            values["verification_completed_at"] = now_datetime()
        if crm_lead and not getattr(convo, "linked_crm_lead", None):
            values["linked_crm_lead"] = crm_lead
        patient_meta = frappe.get_meta("Patient")
        if patient_meta.has_field("sr_medical_department") and meta.has_field("medical_department"):
            medical_department = frappe.db.get_value("Patient", patient, "sr_medical_department")
            if medical_department:
                values.update(
                    {
                        "medical_department": medical_department,
                        "department_source": "patient.sr_medical_department",
                        "department_confidence": 1.0,
                    }
                )
    elif getattr(convo, "linked_crm_lead", None):
        values.update({"party_type": "Lead", "identity_status": "Matched"})
    elif getattr(convo, "linked_reference_doctype", None) in ("CRM Lead", "Lead"):
        values.update({"party_type": "Lead", "identity_status": "Matched"})
    else:
        values.update({"party_type": "Unknown", "identity_status": "Unverified"})

    values = {key: value for key, value in values.items() if meta.has_field(key)}
    changed = any(str(getattr(convo, key, None) or "") != str(value or "") for key, value in values.items())
    if changed:
        frappe.db.set_value("Chat Conversation", conversation, values, update_modified=False)
    return {"changed": changed, "patient": patient, "values": values, "source": patient_source or source}


def reconcile_patient_conversations(doc, method=None) -> None:
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
    patient = doc.get("patient") if doc else None
    lead = doc.get("sr_source_crm_lead") if doc else None
    if not patient:
        return
    filters = []
    if lead:
        filters.append({"linked_crm_lead": lead})
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
    """Verify when chat, supplied, and linked Patient phones all match."""
    if not conversation or not message:
        return {"verified": False, "reason": "conversation_or_message_missing"}
    if not frappe.db.exists("Chat Conversation", conversation):
        return {"verified": False, "reason": "conversation_not_found"}
    if not frappe.db.exists("Chat Message", message):
        return {"verified": False, "reason": "message_not_found"}

    convo = frappe.get_doc("Chat Conversation", conversation)
    if getattr(convo, "identity_status", None) == "Verified":
        return {
            "verified": True,
            "reason": "already_verified",
            "patient": getattr(convo, "linked_patient", None),
        }

    patient, _ = _trusted_patient(convo, None, getattr(convo, "linked_crm_lead", None))
    if not patient:
        return {"verified": False, "reason": "linked_patient_missing"}

    msg = frappe.get_doc("Chat Message", message)
    if str(msg.conversation) != str(conversation) or msg.direction != "Inbound":
        return {"verified": False, "reason": "message_not_current_inbound"}

    chat_phone = _normalized_phone(
        frappe.db.get_value("Chat Contact", convo.contact, "phone_number")
        if getattr(convo, "contact", None)
        else None
    )
    supplied_phones = _phones_from_text(msg.body)
    if not chat_phone:
        return {"verified": False, "reason": "chat_phone_missing", "patient": patient}
    if chat_phone not in supplied_phones:
        return {"verified": False, "reason": "supplied_phone_mismatch", "patient": patient}

    patient_phone_field = _matching_patient_phone_field(patient, chat_phone)
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
        "reason": "three_way_phone_match",
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
    """Match the current chat number to the linked Patient's registered number."""
    if not conversation or not frappe.db.exists("Chat Conversation", conversation):
        frappe.throw("Chat Conversation was not found.", frappe.PermissionError)

    convo = frappe.get_doc("Chat Conversation", conversation)
    linked_patient, _source = _trusted_patient(
        convo,
        None,
        getattr(convo, "linked_crm_lead", None),
    )
    if not linked_patient or linked_patient != patient:
        frappe.throw(
            "The patient does not match the conversation identity.",
            frappe.PermissionError,
        )

    chat_phone = _normalized_phone(
        frappe.db.get_value("Chat Contact", convo.contact, "phone_number")
        if getattr(convo, "contact", None)
        else None
    )
    if not chat_phone:
        return {"verified": False, "reason": "chat_phone_missing"}

    patient_phone_field = _matching_patient_phone_field(patient, chat_phone)
    if not patient_phone_field:
        return {"verified": False, "reason": "patient_phone_mismatch"}

    identity = reconcile_conversation_identity(
        conversation,
        patient=patient,
        source="patient_verification_agent",
        verified=True,
    )
    from wa_chat_hub.agent_router import persist_agent_route, resolve_agent_route

    route = resolve_agent_route(conversation)
    persist_agent_route(conversation, route)
    return {
        "verified": True,
        "reason": "chat_patient_phone_match",
        "patient": patient,
        "patient_phone_field": patient_phone_field,
        "identity": identity,
        "agent_profile": route.agent_profile,
    }


def _phones_from_text(text: str | None) -> set[str]:
    phones: set[str] = set()
    for candidate in PHONE_CANDIDATE_PATTERN.findall(str(text or "")):
        normalized = _normalized_phone(candidate)
        if normalized:
            phones.add(normalized)
    return phones


def _normalized_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 10:
        return None
    return digits[-10:]


def _matching_patient_phone_field(patient: str, phone: str) -> str | None:
    meta = frappe.get_meta("Patient")
    fields = [field for field in VERIFICATION_PATIENT_PHONE_FIELDS if meta.has_field(field)]
    values = frappe.db.get_value("Patient", patient, fields, as_dict=True) or {}
    for field in fields:
        if _normalized_phone(values.get(field)) == phone:
            return field
    return None


def _trusted_patient(convo, patient: str | None, crm_lead: str | None) -> tuple[str | None, str | None]:
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
    lead = crm_lead or getattr(convo, "linked_crm_lead", None)
    if lead and frappe.db.exists("CRM Lead", lead) and frappe.get_meta("CRM Lead").has_field("sr_source_patient"):
        source_patient = frappe.db.get_value("CRM Lead", lead, "sr_source_patient")
        if source_patient and frappe.db.exists("Patient", source_patient):
            return source_patient, "crm_lead_conversion"
    return None, None
