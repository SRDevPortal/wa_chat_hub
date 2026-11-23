"""Sync Interakt / CTWA attribution from Chat Conversation to CRM Lead Meta Details."""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe

from wa_chat_hub.messaging.attribution import extract_attribution
from wa_chat_hub.prompts import get_conversation_crm_lead
from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_get_value,
    safe_ai_set_value,
)

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
    lead_name: str | None = None,
) -> Dict[str, Any]:
    """
    Copy WhatsApp ad attribution onto linked CRM Lead (sr_w_* Meta Details fields).
    Only fills empty lead fields unless force=True.
    """
    if isinstance(conversation, str):
        convo = safe_ai_get_doc("Chat Conversation", conversation)
    else:
        assert_ai_doctype_permission("Chat Conversation", "read")
        convo = conversation

    lead_name = lead_name or get_conversation_crm_lead(convo)
    if not lead_name:
        return {"updated": False, "reason": "no_linked_crm_lead"}

    lead_meta = frappe.get_meta("CRM Lead")
    if not lead_meta.has_field("sr_w_source_id"):
        return {"updated": False, "reason": "crm_lead_meta_fields_missing"}

    attribution = _resolve_attribution(convo, raw_payload)
    if not any(attribution.values()):
        return {"updated": False, "reason": "no_attribution_data", "lead": lead_name}

    existing = safe_ai_get_value(
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

    safe_ai_set_value("CRM Lead", lead_name, updates, update_modified=True)
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


def backfill_all_linked_leads(batch_size=100, after_name="", dry_run=True):
    """Compatibility Bench entry point: preview or apply one explicit batch."""
    from wa_chat_hub.maintenance.history_backfill import run
    return run("crm_lead_meta", batch_size=batch_size, after_name=after_name, dry_run=dry_run)
