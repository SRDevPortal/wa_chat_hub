from __future__ import annotations

import frappe
from frappe import _

from wa_chat_hub.ai.lead_scoring import score_and_sync_conversation, sync_to_linked_lead
from wa_chat_hub.permissions import ensure_can_read_conversation


def _load_payload():
    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()
    return payload


@frappe.whitelist(methods=["POST"])
def create_lead_from_conversation():
    payload = _load_payload()
    conversation = payload.get("conversation")
    if not conversation:
        frappe.throw(_("conversation is required"))

    ensure_can_read_conversation(conversation)
    convo = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", convo.contact)

    doc = frappe.get_doc({
        "doctype": "Lead",
        "lead_name": payload.get("lead_name") or contact.display_name or contact.phone_number,
        "mobile_no": contact.phone_number,
        "source": payload.get("source") or "WhatsApp",
    })
    doc.insert(ignore_permissions=True)

    contact.linked_lead = doc.name
    contact.save(ignore_permissions=True)
    convo.linked_reference_doctype = "Lead"
    convo.linked_reference_name = doc.name
    convo.save(ignore_permissions=True)
    try:
        score_and_sync_conversation(conversation)
        sync_to_linked_lead(conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Lead Sync Failed After Lead Creation")
    return {"success": True, "result": {"doctype": "Lead", "name": doc.name}}


@frappe.whitelist(methods=["POST"])
def create_issue_from_conversation():
    payload = _load_payload()
    conversation = payload.get("conversation")
    subject = payload.get("subject") or "WhatsApp Support Request"
    if not conversation:
        frappe.throw(_("conversation is required"))

    ensure_can_read_conversation(conversation)
    convo = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", convo.contact)

    doc = frappe.get_doc({
        "doctype": "Issue",
        "subject": subject,
        "description": payload.get("description") or convo.last_message_preview or subject,
    })
    doc.insert(ignore_permissions=True)

    convo.linked_reference_doctype = "Issue"
    convo.linked_reference_name = doc.name
    convo.save(ignore_permissions=True)
    return {"success": True, "result": {"doctype": "Issue", "name": doc.name, "contact": contact.name}}


@frappe.whitelist(methods=["POST"])
def create_patient_encounter_from_conversation():
    payload = _load_payload()
    conversation = payload.get("conversation")
    patient = payload.get("patient")
    if not conversation:
        frappe.throw(_("conversation is required"))
    if not patient:
        frappe.throw(_("patient is required"))

    ensure_can_read_conversation(conversation)
    doc = frappe.get_doc({
        "doctype": "Patient Encounter",
        "patient": patient,
    })
    doc.insert(ignore_permissions=True)

    convo = frappe.get_doc("Chat Conversation", conversation)
    convo.linked_reference_doctype = "Patient Encounter"
    convo.linked_reference_name = doc.name
    convo.save(ignore_permissions=True)
    return {"success": True, "result": {"doctype": "Patient Encounter", "name": doc.name}}
