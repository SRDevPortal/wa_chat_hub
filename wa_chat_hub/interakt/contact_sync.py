"""Push ShipKia lead and WhatsApp contact records to Interakt."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import frappe
from frappe import _
from frappe.utils import now_datetime

from wa_chat_hub.channel_resolver import (
    get_or_create_contact_channel_profile,
    _build_interakt_traits,
    _split_interakt_phone,
)
from wa_chat_hub.interakt.contacts_api import extract_interakt_user_id, track_user
from wa_chat_hub.messaging.channel_map import get_pipeline_map
from wa_chat_hub.security import (
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_get_value,
    safe_ai_set_value,
)
from wa_chat_hub.services import get_or_create_contact, normalize_phone


def push_contact_to_interakt(
    channel_account: str,
    contact_name: str,
    *,
    reference_doc=None,
    pipeline_map_row: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Upsert one Chat Contact on Interakt for the given channel account."""
    if not contact_name or not safe_ai_exists("Chat Contact", contact_name):
        frappe.throw(_("Chat Contact {0} not found.").format(contact_name))

    contact_doc = safe_ai_get_doc("Chat Contact", contact_name)
    if not contact_doc.phone_number:
        frappe.throw(_("Chat Contact {0} has no phone number.").format(contact_name))

    account = safe_ai_get_doc("Chat Channel Account", channel_account)
    if pipeline_map_row is None:
        pipeline_map_row = get_pipeline_map(channel_account=channel_account)

    reference_doc = reference_doc or resolve_reference_for_contact(contact_doc)
    country_code, phone_number = _split_interakt_phone(
        contact_doc.phone_number,
        getattr(account, "interakt_default_country_code", None) or "+91",
    )
    if not phone_number:
        frappe.throw(_("No valid WhatsApp phone number for contact {0}.").format(contact_name))

    if reference_doc:
        traits = _build_interakt_traits(contact_doc, reference_doc)
    else:
        traits = {
            "name": contact_doc.display_name or contact_name,
        }
        if contact_doc.source_doctype:
            traits["source_doctype"] = contact_doc.source_doctype
        if contact_doc.source_name:
            traits["source_name"] = contact_doc.source_name
    traits = _enrich_traits_from_pipeline_map(traits, pipeline_map_row)

    payload: Dict[str, Any] = {
        "phoneNumber": phone_number,
        "countryCode": country_code,
        "traits": traits,
    }
    if reference_doc and reference_doc.name:
        payload["traits"]["erp_reference"] = f"{reference_doc.doctype}/{reference_doc.name}"

    tags = _build_interakt_tags(pipeline_map_row)
    if tags:
        payload["tags"] = tags

    profile = get_or_create_contact_channel_profile(
        contact_name,
        channel_account,
        pipeline_map_row.get("sr_lead_pipeline"),
    )

    try:
        result = track_user(channel_account, payload)
    except Exception as exc:
        safe_ai_set_value(
            "Chat Contact Channel Profile",
            profile,
            {"interakt_synced": 0, "last_sync_error": str(exc)[:500]},
        )
        raise

    safe_ai_set_value(
        "Chat Contact Channel Profile",
        profile,
        {
            "interakt_synced": 1,
            "interakt_user_id": extract_interakt_user_id(result),
            "last_synced_on": now_datetime(),
            "last_sync_error": None,
        },
    )
    return {
        "success": True,
        "contact": contact_name,
        "channel_account": channel_account,
        "profile": profile,
        "provider_response": result,
    }


def push_reference_to_interakt(reference_doc, channel_account: Optional[str] = None) -> Dict[str, Any]:
    """Push a CRM Lead to Interakt using pipeline map routing."""
    doctype = reference_doc.doctype
    if doctype != "CRM Lead":
        frappe.throw(_("Unsupported doctype for Interakt push: {0}").format(doctype))

    phone = _phone_from_reference(reference_doc)
    normalized = normalize_phone(phone)
    if not normalized:
        frappe.throw(_("No mobile number on {0} {1}.").format(doctype, reference_doc.name))

    if channel_account:
        pipeline_map_row = get_pipeline_map(channel_account=channel_account)
    else:
        pipeline_map_row = get_pipeline_map(pipeline=reference_doc.get("sr_lead_pipeline"))
        channel_account = pipeline_map_row["chat_channel_account"]

    contact_name = get_or_create_contact(
        phone_number=normalized,
        display_name=_display_name_from_reference(reference_doc),
    )
    safe_ai_set_value(
        "Chat Contact",
        contact_name,
        {"source_doctype": doctype, "source_name": reference_doc.name},
    )

    return push_contact_to_interakt(
        channel_account,
        contact_name,
        reference_doc=reference_doc,
        pipeline_map_row=pipeline_map_row,
    )


def push_pipeline_map_contacts(pipeline_map_name: str) -> Dict[str, Any]:
    """Bulk push CRM Leads and chat contacts for one pipeline map row."""
    row = safe_ai_get_doc("WA Channel Pipeline Map", pipeline_map_name)
    if not row.is_active:
        frappe.throw(_("Pipeline map {0} is not active.").format(pipeline_map_name))

    pipeline_map_row = row.as_dict()
    channel_account = row.chat_channel_account
    stats = {"pushed": 0, "failed": 0, "skipped": 0, "errors": []}

    if safe_ai_exists("DocType", "CRM Lead"):
        for lead_name in _crm_lead_names_for_map(row):
            _push_one(stats, lambda ln=lead_name: push_reference_to_interakt(safe_ai_get_doc("CRM Lead", ln), channel_account))

    for contact_name in _chat_contact_names_for_channel(channel_account):
        _push_one(
            stats,
            lambda cn=contact_name: push_contact_to_interakt(
                channel_account, cn, pipeline_map_row=pipeline_map_row
            ),
        )

    frappe.db.commit()
    return {
        "success": True,
        "pipeline_map": pipeline_map_name,
        "channel_account": channel_account,
        **stats,
    }


def enqueue_push_for_conversation(conversation: str) -> None:
    """Background push after inbound message links a contact."""
    if not conversation or not safe_ai_exists("Chat Conversation", conversation):
        return
    channel_account = safe_ai_get_value("Chat Conversation", conversation, "channel_account")
    if not channel_account or not safe_ai_exists("WA Channel Pipeline Map", {"chat_channel_account": channel_account, "is_active": 1}):
        return

    frappe.enqueue(
        "wa_chat_hub.interakt.contact_sync.push_conversation_contact",
        queue="short",
        conversation=conversation,
        timeout=120,
        now=frappe.flags.in_test,
    )


def push_conversation_contact(conversation: str) -> Optional[Dict[str, Any]]:
    if not conversation or not safe_ai_exists("Chat Conversation", conversation):
        return None

    convo = safe_ai_get_doc("Chat Conversation", conversation)
    if not convo.channel_account or not convo.contact:
        return None

    try:
        pipeline_map_row = get_pipeline_map(channel_account=convo.channel_account)
    except Exception:
        return None

    contact_doc = safe_ai_get_doc("Chat Contact", convo.contact)
    reference_doc = resolve_reference_for_contact(contact_doc)
    if not reference_doc and convo.linked_reference_doctype == "CRM Lead" and convo.linked_reference_name:
        if safe_ai_exists(convo.linked_reference_doctype, convo.linked_reference_name):
            reference_doc = safe_ai_get_doc(convo.linked_reference_doctype, convo.linked_reference_name)

    return push_contact_to_interakt(
        convo.channel_account,
        convo.contact,
        reference_doc=reference_doc,
        pipeline_map_row=pipeline_map_row,
    )


def resolve_reference_for_contact(contact_doc) -> Any:
    source_dt = contact_doc.source_doctype
    source_name = contact_doc.source_name
    if source_dt == "CRM Lead" and source_name and safe_ai_exists(source_dt, source_name):
        return safe_ai_get_doc(source_dt, source_name)

    if getattr(contact_doc, "linked_lead", None) and safe_ai_exists("CRM Lead", contact_doc.linked_lead):
        return safe_ai_get_doc("CRM Lead", contact_doc.linked_lead)
    if getattr(contact_doc, "linked_lead", None) and safe_ai_exists("Lead", contact_doc.linked_lead):
        return safe_ai_get_doc("Lead", contact_doc.linked_lead)

    return None


def _enrich_traits_from_pipeline_map(traits: Dict[str, Any], pipeline_map_row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(traits or {})
    pipeline = pipeline_map_row.get("sr_lead_pipeline")
    if pipeline:
        out.setdefault("sr_lead_pipeline", pipeline)
    return {k: v for k, v in out.items() if v not in (None, "")}


def _build_interakt_tags(pipeline_map_row: Dict[str, Any]) -> List[str]:
    tags = []
    for key in ("sr_lead_pipeline",):
        value = pipeline_map_row.get(key)
        if value:
            tags.append(str(value)[:50])
    return tags[:10]


def _crm_lead_names_for_map(row) -> List[str]:
    if not row.sr_lead_pipeline:
        return []
    meta = frappe.get_meta("CRM Lead")
    if not meta.has_field("sr_lead_pipeline"):
        return []
    filters = {"sr_lead_pipeline": row.sr_lead_pipeline}
    if meta.has_field("sr_is_archived"):
        filters["sr_is_archived"] = 0
    if meta.has_field("converted"):
        filters["converted"] = 0
    for field in ("mobile_no", "phone", "mobile"):
        if meta.has_field(field):
            filters[field] = ["is", "set"]
            break
    return safe_ai_get_all("CRM Lead", filters=filters, pluck="name", limit_page_length=0)


def _chat_contact_names_for_channel(channel_account: str) -> List[str]:
    conversations = safe_ai_get_all(
        "Chat Conversation",
        filters={"channel_account": channel_account},
        pluck="contact",
        limit_page_length=0,
    )
    return list({c for c in conversations if c})


def _phone_from_reference(doc) -> Optional[str]:
    meta = frappe.get_meta(doc.doctype)
    fields = ["mobile_no", "phone", "mobile", "whatsapp_number", "whatsapp_no", "custom_whatsapp_number"]
    for fieldname in fields:
        if meta.has_field(fieldname) and doc.get(fieldname):
            return doc.get(fieldname)
    return None


def _display_name_from_reference(doc) -> str:
    for fieldname in ("lead_name", "full_name", "first_name", "contact_name"):
        if doc.get(fieldname):
            return doc.get(fieldname)
    return doc.name


def _push_one(stats: Dict[str, Any], fn) -> None:
    try:
        fn()
        stats["pushed"] += 1
    except Exception as exc:
        stats["failed"] += 1
        stats["errors"].append(str(exc)[:200])
        frappe.log_error(frappe.get_traceback(), "Interakt Contact Push Failed")
