from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe.utils import cint

from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_doc,
    safe_ai_get_value,
)


PROMPT_FIELDS = (
    "system_prompt",
    "escalation_policy",
    "multilingual_reply_policy",
)

CONVERSATION_MEMORY_POLICY = """
Conversation memory rule:
- Before replying, first use the recent chat history, not only the latest user message.
- Continue naturally from previous user messages and previous assistant replies.
- Respect corrections from the user. If the user already said they want message-only support, do not offer a callback again unless they ask for it.
- Do not repeat questions or offers that were already answered in the recent conversation.
- If the customer does not provide the requested information, do not ask the exact same question a second time; acknowledge and move on or offer the best next step.
- If the customer asks for onboarding, signup, registration, account creation, or an onboarding link, give this exact URL: https://auth.shipkia.com/signup
- Keep ShipKia sales flow simple: welcome first without asking for rate inputs; collect pickup city/PIN, delivery city/PIN, weight, and payment type only after the customer asks for rates.
- Do not ask business type, current aggregator, current rate, RTO, or monthly shipments before giving a rate when the customer is asking for rates.
- After giving a useful answer or rate, ask for extra lead details softly and optionally, with permission language; never make it feel mandatory.
- If business type, store name, current aggregator, monthly shipments, pickup/delivery route, weight, payment mode, current rates, RTO, callback time, preferences, or constraints were already shared, use them in the next reply.
- The conversation must feel continuous, natural, and human-like.
""".strip()


def get_effective_prompt_config(channel_account: Optional[str] = None) -> Any:
    """Merge global WA Chat Hub Settings with per-account overrides (non-empty fields only)."""
    from types import SimpleNamespace

    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
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
    for fieldname in ("system_prompt", "escalation_policy"):
        value = (getattr(config, fieldname, None) or "").strip()
        if value:
            parts.append(value)
    parts.append(CONVERSATION_MEMORY_POLICY)
    return "\n\n".join(parts)


def get_multilingual_policy(config: Any, settings) -> str:
    if cint(getattr(settings, "enable_multilingual_replies", 0)):
        return (getattr(config, "multilingual_reply_policy", None) or "").strip()
    return ""


def get_conversation_crm_lead(conversation: str | Any) -> Optional[str]:
    """Resolve CRM Lead name from conversation (new Link field or legacy fields)."""
    if isinstance(conversation, str):
        convo = safe_ai_get_doc("Chat Conversation", conversation)
    else:
        convo = conversation

    linked_crm_lead = getattr(convo, "linked_crm_lead", None)
    if linked_crm_lead and safe_ai_exists("CRM Lead", linked_crm_lead):
        return _resolve_primary_crm_lead(linked_crm_lead)

    if getattr(convo, "linked_reference_doctype", None) in ("CRM Lead", "Lead"):
        name = getattr(convo, "linked_reference_name", None)
        if name and safe_ai_exists("CRM Lead", name):
            return _resolve_primary_crm_lead(name)
    return None


def set_conversation_crm_lead(convo, lead_name: str) -> None:
    """Link conversation to CRM Lead using Link field + legacy sync."""
    assert_ai_doctype_permission("Chat Conversation", "read")
    if frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
        convo.linked_crm_lead = lead_name
    convo.linked_reference_doctype = "CRM Lead"
    convo.linked_reference_name = lead_name


def get_conversation_linked_reference(convo) -> tuple[Optional[str], Optional[str]]:
    """Return the linked ShipKia lead/customer reference when available."""
    crm_lead = get_conversation_crm_lead(convo)
    if crm_lead:
        return "CRM Lead", crm_lead

    ref_dt = getattr(convo, "linked_reference_doctype", None)
    ref_name = getattr(convo, "linked_reference_name", None)
    if ref_dt and ref_name:
        return ref_dt, ref_name
    return None, None


def _resolve_primary_crm_lead(lead_name: str | None) -> Optional[str]:
    if not lead_name or not safe_ai_exists("CRM Lead", lead_name):
        return None

    try:
        from crm_lead_dedupe.leads.dup_utils import get_primary_lead_name_for_lead

        return get_primary_lead_name_for_lead(lead_name) or lead_name
    except Exception:
        pass

    assert_ai_doctype_permission("CRM Lead", "read")
    if frappe.db.has_column("CRM Lead", "sr_duplicate_of_name"):
        primary = safe_ai_get_value("CRM Lead", lead_name, "sr_duplicate_of_name")
        if primary and safe_ai_exists("CRM Lead", primary):
            return primary
    return lead_name
