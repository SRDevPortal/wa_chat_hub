from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe.utils import cint


PROMPT_FIELDS = (
    "system_prompt",
    "medical_guardrail_policy",
    "escalation_policy",
    "multilingual_reply_policy",
)


def get_effective_prompt_config(channel_account: Optional[str] = None) -> Any:
    """Merge global WA Chat Hub Settings with per-account overrides (non-empty fields only)."""
    from types import SimpleNamespace

    settings = frappe.get_single("WA Chat Hub Settings")
    merged = {field: getattr(settings, field, None) for field in PROMPT_FIELDS}

    if channel_account:
        for row in settings.get("account_prompt_maps") or []:
            if row.chat_channel_account == channel_account and cint(row.is_active):
                for field in PROMPT_FIELDS:
                    value = (getattr(row, field, None) or "").strip()
                    if value:
                        merged[field] = value
                break

    return SimpleNamespace(**merged)


def build_system_prompt_from_config(config: Any) -> str:
    parts = []
    for fieldname in PROMPT_FIELDS[:3]:
        value = (getattr(config, fieldname, None) or "").strip()
        if value:
            parts.append(value)
    return "\n\n".join(parts)


def get_multilingual_policy(config: Any, settings) -> str:
    if cint(getattr(settings, "enable_multilingual_replies", 0)):
        return (getattr(config, "multilingual_reply_policy", None) or "").strip()
    return ""


def get_conversation_crm_lead(conversation: str | Any) -> Optional[str]:
    """Resolve CRM Lead name from conversation (new Link field or legacy fields)."""
    if isinstance(conversation, str):
        convo = frappe.get_doc("Chat Conversation", conversation)
    else:
        convo = conversation

    linked_crm_lead = getattr(convo, "linked_crm_lead", None)
    if linked_crm_lead and frappe.db.exists("CRM Lead", linked_crm_lead):
        return linked_crm_lead

    if getattr(convo, "linked_reference_doctype", None) in ("CRM Lead", "Lead"):
        name = getattr(convo, "linked_reference_name", None)
        if name and frappe.db.exists("CRM Lead", name):
            return name
    return None


def set_conversation_crm_lead(convo, lead_name: str) -> None:
    """Link conversation to CRM Lead using Link field + legacy sync."""
    if frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
        convo.linked_crm_lead = lead_name
    convo.linked_reference_doctype = "CRM Lead"
    convo.linked_reference_name = lead_name


def get_conversation_linked_reference(convo) -> tuple[Optional[str], Optional[str]]:
    """Return (doctype, name) for Patient, Customer, CRM Lead, or other legacy links."""
    crm_lead = get_conversation_crm_lead(convo)
    if crm_lead:
        return "CRM Lead", crm_lead

    ref_dt = getattr(convo, "linked_reference_doctype", None)
    ref_name = getattr(convo, "linked_reference_name", None)
    if ref_dt and ref_name:
        return ref_dt, ref_name
    return None, None
