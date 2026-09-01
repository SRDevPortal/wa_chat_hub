from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from wa_chat_hub.messaging.channel_map import get_pipeline_map, get_pipeline_map_for_lead
from wa_chat_hub.security import (
    safe_ai_get_doc,
    safe_ai_get_value,
    safe_ai_insert,
    safe_ai_set_value,
)
from wa_chat_hub.services import DEFAULT_CONVERSATION_STATUS, get_or_create_contact, normalize_phone


def get_channel_context_for_lead(lead):
    """Legacy name: returns pipeline map row as a simple namespace for callers."""
    row = get_pipeline_map_for_lead(lead)
    return frappe._dict(
        name=row["name"],
        channel_account=row["chat_channel_account"],
        pipeline=row["sr_lead_pipeline"],
        department=None,
    )


def get_or_create_lead_contact(lead) -> str:
    phone = _get_lead_phone(lead)
    normalized_phone = normalize_phone(phone)
    if not normalized_phone:
        frappe.throw(_("No mobile number found for {0} {1}.").format(lead.doctype, lead.name))

    contact_name = get_or_create_contact(
        phone_number=normalized_phone,
        display_name=_get_lead_display_name(lead),
    )
    updates = {
        "source_doctype": lead.doctype,
        "source_name": lead.name,
    }
    if lead.doctype == "Lead" and frappe.get_meta("Chat Contact").has_field("linked_lead"):
        updates["linked_lead"] = lead.name
    safe_ai_set_value("Chat Contact", contact_name, updates)
    return contact_name


def ensure_interakt_contact_for_reference(
    channel_account: str,
    contact: str,
    reference_doc,
    *,
    pipeline: str | None = None,
    allow_unmapped: bool = False,
    pipeline_map_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sync Chat Contact to Interakt (CRM Lead, Patient, etc.)."""
    from wa_chat_hub.interakt.contact_sync import push_contact_to_interakt

    if pipeline_map_row is None:
        try:
            pipeline_map_row = get_pipeline_map(channel_account=channel_account)
        except Exception:
            if allow_unmapped:
                pipeline_map_row = {}

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
    pipeline_row = get_pipeline_map_for_lead(lead)
    channel_account = pipeline_row["chat_channel_account"]
    return get_or_create_lead_conversation_for_channel_account(
        lead,
        channel_account,
        pipeline_map=pipeline_row.get("name"),
        pipeline=pipeline_row.get("sr_lead_pipeline"),
    )


def get_or_create_lead_conversation_for_channel_account(
    lead,
    channel_account: str,
    *,
    pipeline_map: str | None = None,
    pipeline: str | None = None,
) -> dict[str, Any]:
    """Create a lead conversation on an explicitly selected Interakt account."""
    account = safe_ai_get_doc("Chat Channel Account", channel_account)
    if not account.is_active or account.channel_type != "Interakt":
        frappe.throw(_("WhatsApp channel {0} must be an active Interakt account.").format(channel_account))

    contact = get_or_create_lead_contact(lead)
    ensure_interakt_contact_for_reference(
        channel_account,
        contact,
        lead,
        pipeline=pipeline or lead.get("sr_lead_pipeline"),
    )

    conversation, created = _get_or_create_reference_conversation(
        contact=contact,
        channel_account=channel_account,
        reference_doctype=lead.doctype,
        reference_name=lead.name,
        department=_conversation_department_for_account(channel_account),
        defer_reference_link=True,
    )

    try:
        from wa_chat_hub.lead_ai import auto_update_lead_from_conversation

        auto_update_lead_from_conversation(lead.name, conversation=conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Lead AI Update On Open Failed")

    if lead.doctype == "CRM Lead":
        try:
            from wa_chat_hub.messaging.crm_lead_meta import sync_crm_lead_meta_from_conversation

            sync_crm_lead_meta_from_conversation(conversation, lead_name=lead.name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "CRM Lead Meta Sync On Map Failed")

    _link_lead_on_conversation(conversation, lead.doctype, lead.name)

    return {
        "conversation": conversation,
        "pipeline_map": pipeline_map,
        "channel_account": channel_account,
        "contact": contact,
        "created": created,
    }


def get_or_create_patient_contact(patient) -> str:
    frappe.throw(_("Patient contact mapping is disabled for ShipKia customer flow."))

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
    frappe.throw(_("Patient conversation mapping is disabled for ShipKia customer flow."))


def get_or_create_patient_conversation_for_channel_account(
    patient,
    channel_account: str,
    *,
    pipeline_map: str | None = None,
) -> dict[str, Any]:
    frappe.throw(_("Patient conversation mapping is disabled for ShipKia customer flow."))

    account = safe_ai_get_doc("Chat Channel Account", channel_account)
    if not account.is_active or account.channel_type != "Interakt":
        frappe.throw(_("WhatsApp channel {0} must be an active Interakt account.").format(channel_account))

    contact = get_or_create_patient_contact(patient)
    ensure_interakt_contact_for_reference(
        channel_account,
        contact,
        patient,
        allow_unmapped=True,
        pipeline_map_row={
            "sr_medical_department": patient.get("sr_medical_department"),
        },
    )

    conversation, created = _get_or_create_reference_conversation(
        contact=contact,
        channel_account=channel_account,
        reference_doctype="Patient",
        reference_name=patient.name,
        department=_conversation_department_for_account(channel_account),
    )
    try:
        from wa_chat_hub.identity import reconcile_conversation_identity

        reconcile_conversation_identity(
            conversation,
            patient=patient.name,
            source="patient_conversation",
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Patient Identity Sync Failed")

    return {
        "conversation": conversation,
        "pipeline_map": pipeline_map,
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
    defer_reference_link: bool = False,
) -> tuple[str, bool]:
    conversation = _find_conversation_for_contact_on_channel(contact, channel_account, open_only=True)
    if not conversation:
        conversation = _find_conversation_for_contact_on_channel(contact, channel_account, open_only=False)

    if conversation:
        if defer_reference_link:
            return conversation, False
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

    payload = {
        "doctype": "Chat Conversation",
        "channel_account": channel_account,
        "contact": contact,
        "department": department,
        "status": DEFAULT_CONVERSATION_STATUS,
    }
    if not defer_reference_link:
        payload.update(
            {
                "linked_reference_doctype": reference_doctype,
                "linked_reference_name": reference_name,
            }
        )
    doc = frappe.get_doc(payload)
    safe_ai_insert(doc)
    return doc.name, True


def _conversation_department_for_account(channel_account: str) -> str | None:
    return safe_ai_get_value("Chat Channel Account", channel_account, "department")


def _link_lead_on_conversation(conversation: str, lead_doctype: str, lead_name: str) -> None:
    updates = {
        "linked_reference_doctype": lead_doctype,
        "linked_reference_name": lead_name,
    }
    if frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
        updates["linked_crm_lead"] = lead_name if lead_doctype == "CRM Lead" else None
    safe_ai_set_value("Chat Conversation", conversation, updates, update_modified=False)


def _link_crm_lead_on_conversation(conversation: str, lead_name: str) -> None:
    _link_lead_on_conversation(conversation, "CRM Lead", lead_name)


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
