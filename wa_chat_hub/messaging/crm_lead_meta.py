"""Sync Interakt / CTWA attribution from Chat Conversation to CRM Lead Meta Details."""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe

from wa_chat_hub.messaging.attribution import extract_attribution
from wa_chat_hub.prompts import get_conversation_crm_lead

CONVERSATION_TO_LEAD = {
    "source_id": "sr_w_source_id",
    "source_url": "sr_w_source_url",
    "ctwa_clid": "sr_w_ctwa_clid",
}


def sync_crm_lead_meta_from_conversation(
    conversation: str | Any,
    *,
    raw_payload: Any = None,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Copy WhatsApp ad attribution onto linked CRM Lead (sr_w_* Meta Details fields).
    Only fills empty lead fields unless force=True.
    """
    if isinstance(conversation, str):
        convo = frappe.get_doc("Chat Conversation", conversation)
    else:
        convo = conversation

    lead_name = get_conversation_crm_lead(convo)
    if not lead_name:
        return {"updated": False, "reason": "no_linked_crm_lead"}

    lead_meta = frappe.get_meta("CRM Lead")
    if not lead_meta.has_field("sr_w_source_id"):
        return {"updated": False, "reason": "crm_lead_meta_fields_missing"}

    attribution = _resolve_attribution(convo, raw_payload)
    if not any(attribution.values()):
        return {"updated": False, "reason": "no_attribution_data", "lead": lead_name}

    existing = frappe.db.get_value(
        "CRM Lead",
        lead_name,
        list(CONVERSATION_TO_LEAD.values()),
        as_dict=True,
    ) or {}

    updates: Dict[str, str] = {}
    for conv_field, lead_field in CONVERSATION_TO_LEAD.items():
        value = (attribution.get(conv_field) or "").strip()
        if not value:
            continue
        if force or not (existing.get(lead_field) or "").strip():
            updates[lead_field] = value

    if not updates:
        return {"updated": False, "reason": "lead_already_has_meta", "lead": lead_name}

    frappe.db.set_value("CRM Lead", lead_name, updates, update_modified=True)
    return {"updated": True, "lead": lead_name, "fields": updates}


def _resolve_attribution(convo: Any, raw_payload: Any) -> Dict[str, Optional[str]]:
    data = extract_attribution(_coerce_payload_dict(raw_payload)) if raw_payload else {}
    for conv_field in CONVERSATION_TO_LEAD:
        if not data.get(conv_field):
            value = getattr(convo, conv_field, None)
            if value not in (None, ""):
                data[conv_field] = str(value)
    return data


def _coerce_payload_dict(raw_payload: Any) -> Dict[str, Any]:
    if isinstance(raw_payload, dict):
        return raw_payload
    if isinstance(raw_payload, str) and raw_payload.strip():
        import json

        try:
            parsed = json.loads(raw_payload)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def backfill_all_linked_leads() -> Dict[str, int]:
    """bench execute helper: copy conversation attribution to CRM Leads."""
    if not frappe.db.exists("DocType", "Chat Conversation"):
        return {"updated": 0, "skipped": 0}

    meta = frappe.get_meta("Chat Conversation")
    filters = {"status": ["!=", "Closed"]}
    fields = ["name"]
    if meta.has_field("linked_crm_lead"):
        filters["linked_crm_lead"] = ["is", "set"]
        fields.append("linked_crm_lead")
    else:
        filters["linked_reference_doctype"] = "CRM Lead"
        filters["linked_reference_name"] = ["is", "set"]
        fields.extend(["linked_reference_doctype", "linked_reference_name"])

    updated = 0
    skipped = 0
    for row in frappe.get_all("Chat Conversation", filters=filters, fields=fields, limit_page_length=0):
        result = sync_crm_lead_meta_from_conversation(row.name, force=False)
        if result.get("updated"):
            updated += 1
        else:
            skipped += 1
    frappe.db.commit()
    return {"updated": updated, "skipped": skipped}
