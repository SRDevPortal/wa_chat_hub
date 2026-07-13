from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import now_datetime


PATIENT_PHONE_FIELDS = ("mobile", "mobile_no", "phone", "custom_whatsapp_number")


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
        values.update(
            {
                "linked_patient": patient,
                "party_type": "Patient",
                "identity_status": "Verified" if verified else "Matched",
                "linked_reference_doctype": "Patient",
                "linked_reference_name": patient,
                "routing_reason": f"patient_identity:{patient_source or source}",
            }
        )
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
