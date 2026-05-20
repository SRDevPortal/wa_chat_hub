from __future__ import annotations

from typing import Any

import frappe
import requests
from frappe import _
from frappe.utils import now_datetime

from wa_chat_hub.services import DEFAULT_CONVERSATION_STATUS, get_or_create_contact, normalize_phone


INTERAKT_TRACK_USER_URL = "https://api.interakt.ai/v1/public/track/users/"


def get_channel_context_for_lead(lead):
    pipeline = lead.get("sr_lead_pipeline")
    if not pipeline:
        frappe.throw(_("CRM Lead {0} does not have a pipeline.").format(lead.name))

    matches = frappe.get_all(
        "WA Channel Context",
        filters={"pipeline": pipeline, "is_active": 1},
        fields=["name", "context_name", "channel_account", "pipeline", "department"],
        limit_page_length=2,
    )

    if not matches:
        frappe.throw(_("No active WA Channel Context configured for pipeline {0}.").format(pipeline))
    if len(matches) > 1:
        frappe.throw(_("Pipeline {0} has multiple active WA Channel Context records.").format(pipeline))

    context = frappe.get_doc("WA Channel Context", matches[0].name)
    account = frappe.get_cached_doc("Chat Channel Account", context.channel_account)
    if not account.is_active:
        frappe.throw(_("Mapped WhatsApp channel {0} is not active.").format(account.name))
    if account.channel_type != "Interakt":
        frappe.throw(_("Mapped WhatsApp channel {0} must be an Interakt account.").format(account.name))

    return context


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
    frappe.db.set_value("Chat Contact", contact_name, updates)
    return contact_name


def ensure_interakt_contact_for_lead(channel_account: str, contact: str, lead) -> dict[str, Any]:
    account = frappe.get_doc("Chat Channel Account", channel_account)
    if account.channel_type != "Interakt":
        frappe.throw(_("Channel Account {0} is not an Interakt account.").format(channel_account))

    api_key = account.get_password("interakt_api_key")
    if not api_key:
        frappe.throw(_("Interakt API Key is not configured for {0}.").format(channel_account))

    contact_doc = frappe.get_doc("Chat Contact", contact)
    country_code, phone_number = _split_interakt_phone(
        contact_doc.phone_number,
        getattr(account, "interakt_default_country_code", None) or "+91",
    )
    if not phone_number:
        frappe.throw(_("No valid WhatsApp phone number found for contact {0}.").format(contact))

    payload = {
        "phoneNumber": phone_number,
        "countryCode": country_code,
        "traits": _build_interakt_traits(contact_doc, lead),
    }
    profile = get_or_create_contact_channel_profile(contact, channel_account, lead.get("sr_lead_pipeline"))

    try:
        response = requests.post(
            INTERAKT_TRACK_USER_URL,
            headers={
                "Authorization": f"Basic {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
    except Exception as exc:
        frappe.db.set_value(
            "Chat Contact Channel Profile",
            profile,
            {
                "interakt_synced": 0,
                "last_sync_error": str(exc)[:500],
            },
        )
        raise

    raw_response = response.text
    if not response.ok:
        frappe.log_error(
            f"Interakt Contact Sync Error {response.status_code}: {raw_response}\nPayload: {frappe.as_json(payload)}",
            "Interakt Contact Sync Failed",
        )
        frappe.db.set_value(
            "Chat Contact Channel Profile",
            profile,
            {
                "interakt_synced": 0,
                "last_sync_error": raw_response[:500],
            },
        )
        response.raise_for_status()

    result = response.json() if response.content else {}
    external_user_id = _extract_interakt_user_id(result)
    frappe.db.set_value(
        "Chat Contact Channel Profile",
        profile,
        {
            "interakt_synced": 1,
            "interakt_user_id": external_user_id,
            "last_synced_on": now_datetime(),
            "last_sync_error": None,
        },
    )
    frappe.logger("wa_chat_hub").info(
        {
            "message": "Interakt contact ensured",
            "channel_account": channel_account,
            "contact": contact,
            "lead": lead.name,
            "synced_at": str(now_datetime()),
        }
    )
    return {
        "success": True,
        "channel_account": channel_account,
        "contact": contact,
        "profile": profile,
        "provider_response": result,
    }


def get_or_create_mapped_lead_conversation(lead) -> dict[str, Any]:
    context = get_channel_context_for_lead(lead)
    channel_account = context.channel_account
    contact = get_or_create_lead_contact(lead)
    ensure_interakt_contact_for_lead(channel_account, contact, lead)

    conversation = _find_conversation_for_contact_on_channel(contact, channel_account, open_only=True)
    if not conversation:
        conversation = _find_conversation_for_contact_on_channel(contact, channel_account, open_only=False)

    created = False
    if not conversation:
        doc = frappe.get_doc(
            {
                "doctype": "Chat Conversation",
                "channel_account": channel_account,
                "contact": contact,
                "department": context.department or frappe.db.get_value("Chat Channel Account", channel_account, "department"),
                "status": DEFAULT_CONVERSATION_STATUS,
                "linked_reference_doctype": "CRM Lead",
                "linked_reference_name": lead.name,
            }
        )
        doc.insert(ignore_permissions=True)
        conversation = doc.name
        created = True
    else:
        updates = {}
        existing_reference = frappe.db.get_value(
            "Chat Conversation",
            conversation,
            ["linked_reference_doctype", "linked_reference_name"],
            as_dict=True,
        )
        if not existing_reference.linked_reference_doctype:
            updates["linked_reference_doctype"] = "CRM Lead"
        if not existing_reference.linked_reference_name:
            updates["linked_reference_name"] = lead.name
        if updates:
            frappe.db.set_value("Chat Conversation", conversation, updates)

    try:
        from wa_chat_hub.lead_ai import auto_update_lead_from_conversation

        auto_update_lead_from_conversation(lead.name, conversation=conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Lead AI Update On Open Failed")

    return {
        "conversation": conversation,
        "channel_context": context.name,
        "channel_account": channel_account,
        "contact": contact,
        "created": created,
    }


def get_or_create_contact_channel_profile(contact: str, channel_account: str, pipeline: str | None = None) -> str:
    existing = frappe.db.get_value(
        "Chat Contact Channel Profile",
        {"contact": contact, "channel_account": channel_account},
        "name",
    )
    if existing:
        if pipeline:
            frappe.db.set_value("Chat Contact Channel Profile", existing, "pipeline", pipeline)
        return existing

    doc = frappe.get_doc(
        {
            "doctype": "Chat Contact Channel Profile",
            "contact": contact,
            "channel_account": channel_account,
            "pipeline": pipeline,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def _find_conversation_for_contact_on_channel(contact: str, channel_account: str, open_only: bool) -> str | None:
    filters: dict[str, Any] = {
        "contact": contact,
        "channel_account": channel_account,
    }
    if open_only:
        filters["status"] = ["!=", "Closed"]

    return frappe.db.get_value(
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


def _build_interakt_traits(contact_doc, lead) -> dict[str, Any]:
    traits = {
        "name": contact_doc.display_name or _get_lead_display_name(lead),
        "source_doctype": "CRM Lead",
        "source_name": lead.name,
        "sr_lead_pipeline": lead.get("sr_lead_pipeline"),
    }

    for fieldname in ("email", "email_id", "source", "status"):
        if lead.get(fieldname):
            traits[fieldname] = lead.get(fieldname)

    return {key: value for key, value in traits.items() if value not in (None, "")}


def _extract_interakt_user_id(result: dict[str, Any]) -> str | None:
    for source in (result, result.get("data") if isinstance(result, dict) else None, result.get("result") if isinstance(result, dict) else None):
        if isinstance(source, dict):
            value = source.get("userId") or source.get("user_id") or source.get("id")
            if value:
                return str(value)
    return None
