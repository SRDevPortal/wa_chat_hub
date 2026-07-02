from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from wa_chat_hub.messaging.channel_map import get_pipeline_map
from wa_chat_hub.prompts import set_conversation_crm_lead
from wa_chat_hub.security import (
    safe_ai_get_doc,
    safe_ai_get_value,
    safe_ai_insert,
    safe_ai_save,
    safe_ai_set_value,
)
from wa_chat_hub.services import DEFAULT_CONVERSATION_STATUS, get_or_create_contact, normalize_phone


def get_channel_context_for_lead(lead):
    """Legacy name: returns pipeline map row as a simple namespace for callers."""
    pipeline = lead.get("sr_lead_pipeline")
    if not pipeline:
        frappe.throw(_("CRM Lead {0} does not have a pipeline.").format(lead.name))
    row = get_pipeline_map(pipeline=pipeline)
    return frappe._dict(
        name=row["name"],
        channel_account=row["chat_channel_account"],
        pipeline=row["sr_lead_pipeline"],
        department=row.get("sr_medical_department"),
    )


def get_or_create_lead_contact(lead) -> str:
    phone = _get_lead_phone(lead)
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        frappe.throw(_("No mobile number found for CRM Lead {0}.").format(lead.name))

    contact_name = get_or_create_contact(
        phone_number=normalized_phone,
        display_name=_get_lead_display_name(lead),
    )
    updates = {
        "source_doctype": "CRM Lead",
        "source_name": lead.name,
    }
    safe_ai_set_value("Chat Contact", contact_name, updates)
    return contact_name


def ensure_interakt_contact_for_reference(
    channel_account: str,
    contact: str,
    reference_doc,
    *,
    pipeline: str | None = None,
) -> dict[str, Any]:
    """Sync Chat Contact to Interakt (CRM Lead, Patient, etc.)."""
    from wa_chat_hub.interakt.contact_sync import push_contact_to_interakt

    pipeline_map_row = None
    try:
        pipeline_map_row = get_pipeline_map(channel_account=channel_account)
    except Exception:
        pass

    return push_contact_to_interakt(
        channel_account,
        contact,
        reference_doc=reference_doc,
        pipeline_map_row=pipeline_map_row,
    )


def ensure_interakt_contact_for_lead(channel_account: str, contact: str, lead) -> dict[str, Any]:
    return ensure_interakt_contact_for_reference(
        channel_account,
        contact,
        lead,
        pipeline=lead.get("sr_lead_pipeline"),
    )


def get_or_create_mapped_lead_conversation(lead) -> dict[str, Any]:
    pipeline_row = get_pipeline_map(pipeline=lead.get("sr_lead_pipeline"))
    channel_account = pipeline_row["chat_channel_account"]
    contact = get_or_create_lead_contact(lead)
    ensure_interakt_contact_for_reference(
        channel_account,
        contact,
        lead,
        pipeline=pipeline_row.get("sr_lead_pipeline"),
    )

    conversation, created = _get_or_create_reference_conversation(
        contact=contact,
        channel_account=channel_account,
        reference_doctype="CRM Lead",
        reference_name=lead.name,
        department=_conversation_department_for_account(channel_account),
    )
    _link_crm_lead_on_conversation(conversation, lead.name)

    try:
        from wa_chat_hub.lead_ai import auto_update_lead_from_conversation

        auto_update_lead_from_conversation(lead.name, conversation=conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Lead AI Update On Open Failed")

    try:
        from wa_chat_hub.messaging.crm_lead_meta import sync_crm_lead_meta_from_conversation

        sync_crm_lead_meta_from_conversation(conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "CRM Lead Meta Sync On Map Failed")

    return {
        "conversation": conversation,
        "pipeline_map": pipeline_row["name"],
        "channel_account": channel_account,
        "contact": contact,
        "created": created,
    }


def get_or_create_patient_contact(patient) -> str:
    phone = _get_patient_phone(patient)
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        frappe.throw(_("No mobile number found for Patient {0}.").format(patient.name))

    contact_name = get_or_create_contact(
        phone_number=normalized_phone,
        display_name=_get_patient_display_name(patient),
    )
    safe_ai_set_value(
        "Chat Contact",
        contact_name,
        {
            "source_doctype": "Patient",
            "source_name": patient.name,
        },
    )
    return contact_name


def get_or_create_mapped_patient_conversation(patient) -> dict[str, Any]:
    pipeline_row = get_pipeline_map(medical_department=patient.get("sr_medical_department"))
    channel_account = pipeline_row["chat_channel_account"]
    contact = get_or_create_patient_contact(patient)
    ensure_interakt_contact_for_reference(
        channel_account,
        contact,
        patient,
        pipeline=pipeline_row.get("sr_lead_pipeline"),
    )

    conversation, created = _get_or_create_reference_conversation(
        contact=contact,
        channel_account=channel_account,
        reference_doctype="Patient",
        reference_name=patient.name,
        department=_conversation_department_for_account(channel_account),
    )

    return {
        "conversation": conversation,
        "pipeline_map": pipeline_row["name"],
        "channel_account": channel_account,
        "contact": contact,
        "created": created,
    }


def get_or_create_contact_channel_profile(contact: str, channel_account: str, pipeline: str | None = None) -> str:
    existing = safe_ai_get_value(
        "Chat Contact Channel Profile",
        {"contact": contact, "channel_account": channel_account},
        "name",
    )
    if existing:
        if pipeline:
            safe_ai_set_value("Chat Contact Channel Profile", existing, "pipeline", pipeline)
        return existing

    doc = frappe.get_doc(
        {
            "doctype": "Chat Contact Channel Profile",
            "contact": contact,
            "channel_account": channel_account,
            "pipeline": pipeline,
        }
    )
    safe_ai_insert(doc)
    return doc.name


def _get_or_create_reference_conversation(
    *,
    contact: str,
    channel_account: str,
    reference_doctype: str,
    reference_name: str,
    department: str | None = None,
) -> tuple[str, bool]:
    conversation = _find_conversation_for_contact_on_channel(contact, channel_account, open_only=True)
    if not conversation:
        conversation = _find_conversation_for_contact_on_channel(contact, channel_account, open_only=False)

    if conversation:
        updates = {}
        existing = safe_ai_get_value(
            "Chat Conversation",
            conversation,
            ["linked_reference_doctype", "linked_reference_name"],
            as_dict=True,
        )
        if not existing.linked_reference_doctype:
            updates["linked_reference_doctype"] = reference_doctype
        if not existing.linked_reference_name:
            updates["linked_reference_name"] = reference_name
        if updates:
            safe_ai_set_value("Chat Conversation", conversation, updates)
        return conversation, False

    doc = frappe.get_doc(
        {
            "doctype": "Chat Conversation",
            "channel_account": channel_account,
            "contact": contact,
            "department": department,
            "status": DEFAULT_CONVERSATION_STATUS,
            "linked_reference_doctype": reference_doctype,
            "linked_reference_name": reference_name,
        }
    )
    safe_ai_insert(doc)
    return doc.name, True


def _conversation_department_for_account(channel_account: str) -> str | None:
    return safe_ai_get_value("Chat Channel Account", channel_account, "department")


def _link_crm_lead_on_conversation(conversation: str, lead_name: str) -> None:
    if not frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
        return
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    set_conversation_crm_lead(convo, lead_name)
    safe_ai_save(convo)


def _find_conversation_for_contact_on_channel(contact: str, channel_account: str, open_only: bool) -> str | None:
    filters: dict[str, Any] = {
        "contact": contact,
        "channel_account": channel_account,
    }
    if open_only:
        filters["status"] = ["!=", "Closed"]

    return safe_ai_get_value(
        "Chat Conversation",
        filters,
        "name",
        order_by="modified desc",
    )


def _get_lead_phone(lead) -> str | None:
    meta = frappe.get_meta(lead.doctype)
    for fieldname in ("mobile_no", "phone", "mobile", "whatsapp_number", "whatsapp_no", "custom_whatsapp_number"):
        if meta.has_field(fieldname) and lead.get(fieldname):
            return lead.get(fieldname)
    return None


def _get_lead_display_name(lead) -> str:
    for fieldname in ("lead_name", "full_name", "first_name", "contact_name"):
        if lead.get(fieldname):
            return lead.get(fieldname)
    return lead.name


def _get_patient_phone(patient) -> str | None:
    meta = frappe.get_meta("Patient")
    for fieldname in ("mobile", "mobile_no", "phone", "custom_whatsapp_number"):
        if meta.has_field(fieldname) and patient.get(fieldname):
            return patient.get(fieldname)
    return None


def _get_patient_display_name(patient) -> str:
    for fieldname in ("patient_name", "first_name"):
        if patient.get(fieldname):
            return patient.get(fieldname)
    return patient.name


def _split_interakt_phone(phone: str, default_country_code: str) -> tuple[str, str]:
    country_code = str(default_country_code or "+91").strip()
    if not country_code.startswith("+"):
        country_code = f"+{country_code}"

    phone_digits = normalize_phone(phone)
    country_digits = normalize_phone(country_code)
    if country_digits and phone_digits.startswith(country_digits) and len(phone_digits) > len(country_digits):
        phone_digits = phone_digits[len(country_digits):]

    if phone_digits.startswith("0"):
        phone_digits = phone_digits.lstrip("0")

    return country_code, phone_digits


def _build_interakt_traits(contact_doc, reference_doc) -> dict[str, Any]:
    doctype = reference_doc.doctype
    if doctype == "Patient":
        display = _get_patient_display_name(reference_doc)
        traits = {
            "name": contact_doc.display_name or display,
            "source_doctype": "Patient",
            "source_name": reference_doc.name,
            "sr_medical_department": reference_doc.get("sr_medical_department"),
        }
        if reference_doc.get("sr_patient_id"):
            traits["sr_patient_id"] = reference_doc.get("sr_patient_id")
    else:
        display = _get_lead_display_name(reference_doc)
        traits = {
            "name": contact_doc.display_name or display,
            "source_doctype": doctype,
            "source_name": reference_doc.name,
            "sr_lead_pipeline": reference_doc.get("sr_lead_pipeline"),
        }
        for fieldname in ("email", "email_id", "source", "status"):
            if reference_doc.get(fieldname):
                traits[fieldname] = reference_doc.get(fieldname)

    return {key: value for key, value in traits.items() if value not in (None, "")}

