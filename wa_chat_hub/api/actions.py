from __future__ import annotations

import frappe
from frappe import _

from wa_chat_hub.ai.lead_scoring import score_and_sync_conversation, sync_to_linked_lead
from wa_chat_hub.permissions import ensure_can_read_conversation
from wa_chat_hub.prompts import get_conversation_crm_lead, set_conversation_crm_lead
from wa_chat_hub.security import safe_ai_exists
from wa_chat_hub.services import (
    _create_lead_for_inbound,
    _finalize_crm_lead_after_inbound,
    _find_primary_crm_lead_by_phone,
    conversation_update_lock,
    normalize_phone,
)


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
def create_crm_lead_from_conversation():
    """Create or reuse one CRM Lead and link it to the current conversation in place."""
    payload = _load_payload()
    conversation = payload.get("conversation")
    if not conversation:
        frappe.throw(_("conversation is required"))

    ensure_can_read_conversation(conversation)
    if not safe_ai_exists("DocType", "CRM Lead"):
        frappe.throw(_("CRM Lead is not installed."))

    with conversation_update_lock(conversation):
        convo = frappe.get_doc("Chat Conversation", conversation)
        contact = frappe.get_doc("Chat Contact", convo.contact)
        phone_number = normalize_phone(contact.phone_number)
        if not phone_number:
            frappe.throw(_("The WhatsApp contact has no valid phone number."))

        lead_name = get_conversation_crm_lead(convo)
        created = False
        if not lead_name:
            lead_name = _find_primary_crm_lead_by_phone(phone_number)
        if not lead_name:
            lead_name = _create_lead_for_inbound(
                doctype="CRM Lead",
                phone_number=phone_number,
                display_name=payload.get("lead_name") or contact.display_name or phone_number,
                channel_account=convo.channel_account,
            )
            created = bool(lead_name)
        if not lead_name:
            frappe.throw(
                _(
                    "CRM Lead could not be created. Verify the Channel Account pipeline mapping "
                    "and CRM Lead required fields."
                )
            )

        contact_updates = {"source_doctype": "CRM Lead", "source_name": lead_name}
        if frappe.get_meta("Chat Contact").has_field("linked_lead"):
            contact_updates["linked_lead"] = None
        frappe.db.set_value("Chat Contact", contact.name, contact_updates, update_modified=False)

        set_conversation_crm_lead(convo, lead_name)
        conversation_updates = {
            "linked_reference_doctype": "CRM Lead",
            "linked_reference_name": lead_name,
        }
        if frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
            conversation_updates["linked_crm_lead"] = lead_name
        frappe.db.set_value(
            "Chat Conversation", conversation, conversation_updates, update_modified=False
        )
        for fieldname, value in conversation_updates.items():
            setattr(convo, fieldname, value)

        _finalize_crm_lead_after_inbound(conversation, lead_name, convo=convo)
        try:
            score_and_sync_conversation(conversation)
            sync_to_linked_lead(conversation)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "CRM Lead Sync Failed After Chat Link")

    return {
        "success": True,
        "result": {"doctype": "CRM Lead", "name": lead_name, "created": created},
    }


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
