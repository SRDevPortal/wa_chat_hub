from __future__ import annotations

import re
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
    "medical_guardrail_policy",
    "escalation_policy",
    "multilingual_reply_policy",
)

DEFAULT_ACCOUNT_MCP_MAX_TOOL_CALLS = 2

CONVERSATION_MEMORY_POLICY = """
Conversation memory rule:
- Before replying, first use the recent chat history, not only the latest user message.
- Continue naturally from previous user messages and previous assistant replies.
- Respect corrections from the user. If the user already said they want message-only support, do not offer a callback or consultation arrangement again unless they ask for it.
- Do not repeat questions, requests for reports, or offers that were already answered in the recent conversation.
- If reports, symptoms, history, preferences, or constraints were already shared, use them in the next reply.
- The conversation must feel continuous, natural, and human-like.

Senior sales conversation rule:
- Behave like an experienced ShipKia senior sales consultant, not like a form.
- Use short WhatsApp-friendly points only when they genuinely make the answer easier to scan, such as services, workflows, comparisons, rates, or onboarding benefits.
- For simple acknowledgements, qualification follow-ups, objections, and normal sales conversation, reply naturally in one short paragraph instead of forcing bullet points.
- When using points, keep them crisp: usually 2-4 points, then one next question or one clear next action.
- Guide the customer toward onboarding naturally: understand their pain, qualify one detail at a time, connect ShipKia benefits to that pain, and move them toward signup/demo when they show fit or intent.
- If the customer asks for onboarding/signup/account creation directly, share the ShipKia onboarding link without blocking on missing qualification details: https://auth.shipkia.com/signup
- If the recent context is onboarding/signup/account setup and the customer says "link send/share/do/dedo", treat it as an onboarding-link request and share the same link.
- Never say that ShipKia cannot provide an onboarding link.
- If the customer only greets with hello/hi, greet back warmly and ask how you can help; do not pitch, qualify, or ask for shipping details in that first reply.
- Ask only one question in one reply.
- Ask for only one missing detail in one reply.
- Collect missing details step by step across messages. Do not ask for three or more details together.
- Never ask business/store name, monthly shipments, and current shipping provider/aggregator in the same reply.
- Never ask pickup city, delivery city, and weight in the same reply.
- If several details are missing, choose the single next most useful detail for the current intent.
- Keep replies short, warm, and conversational. Acknowledge what the customer already shared, then ask the next question.
- Treat phrases like "D zone 38 padta hai", "38 ka padta hai", or "40 percent RTO" as useful customer facts, not refusal.
- If the customer corrects you, accept the correction directly and update your understanding before asking the next small question.
- Whenever the customer asks anything general about ShipKia, such as what ShipKia is, services, features, or how it works, always mention order confirmation and NDR workflows along with shipping/rates/COD/tracking where relevant.
- ShipKia NDR workflow: first send a WhatsApp message to the buyer; if the buyer does not respond, trigger an IVR call follow-up. On WhatsApp, buyers can confirm or request changes such as address/phone/update details where applicable.
- ShipKia order confirmation workflow: when an order is patched/created in the system, order confirmation is sent to the buyer before dispatch so avoidable RTO can be reduced.
- Once enough information is available, summarize briefly and tell the customer the next action.
""".strip()


def get_effective_prompt_config(channel_account: Optional[str] = None) -> Any:
    """Merge global WA Chat Hub Settings with per-account overrides (non-empty fields only)."""
    from types import SimpleNamespace

    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    merged = {field: getattr(settings, field, None) for field in PROMPT_FIELDS}
    merged["account_mcp_tools_enabled"] = False
    merged["account_mcp_tool_names"] = set()
    merged["account_max_tool_calls"] = 0

    if channel_account:
        for row in settings.get("account_prompt_maps") or []:
            if row.chat_channel_account == channel_account and cint(row.is_active):
                for field in PROMPT_FIELDS:
                    value = (getattr(row, field, None) or "").strip()
                    if value:
                        merged[field] = value
                if cint(getattr(row, "allow_mcp_tools", 0)):
                    merged["account_mcp_tools_enabled"] = True
                    merged["account_mcp_tool_names"] = _parse_mcp_tool_names(
                        getattr(row, "mcp_tool_names", None)
                    )
                    merged["account_max_tool_calls"] = max(
                        0,
                        min(
                            5,
                            cint(
                                getattr(row, "max_tool_calls", 0)
                                or DEFAULT_ACCOUNT_MCP_MAX_TOOL_CALLS
                            ),
                        ),
                    )
                break

    return SimpleNamespace(**merged)


def build_system_prompt_from_config(config: Any) -> str:
    parts = []
    for fieldname in PROMPT_FIELDS[:3]:
        value = (getattr(config, fieldname, None) or "").strip()
        if value:
            parts.append(value)
    parts.append(CONVERSATION_MEMORY_POLICY)
    return "\n\n".join(parts)


def get_multilingual_policy(config: Any, settings) -> str:
    if cint(getattr(settings, "enable_multilingual_replies", 0)):
        return (getattr(config, "multilingual_reply_policy", None) or "").strip()
    return ""


def get_account_mcp_tool_names(config: Any) -> set[str]:
    return {
        str(tool_name).strip()
        for tool_name in (getattr(config, "account_mcp_tool_names", None) or set())
        if str(tool_name).strip()
    }


def is_account_mcp_tools_enabled(config: Any) -> bool:
    return bool(cint(getattr(config, "account_mcp_tools_enabled", 0)))


def get_account_max_tool_calls(config: Any) -> int:
    return max(0, min(5, cint(getattr(config, "account_max_tool_calls", 0) or 0)))


def get_conversation_crm_lead(conversation: str | Any) -> Optional[str]:
    """Resolve CRM Lead name from conversation (new Link field or legacy fields)."""
    if isinstance(conversation, str):
        convo = safe_ai_get_doc("Chat Conversation", conversation)
    else:
        convo = conversation

    linked_crm_lead = getattr(convo, "linked_crm_lead", None)
    if linked_crm_lead and safe_ai_exists("CRM Lead", linked_crm_lead):
        return _resolve_primary_crm_lead(linked_crm_lead)

    if getattr(convo, "linked_reference_doctype", None) == "CRM Lead":
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
    """Return (doctype, name) for Customer, Lead, CRM Lead, or other links."""
    ref_dt = getattr(convo, "linked_reference_doctype", None)
    ref_name = getattr(convo, "linked_reference_name", None)

    crm_lead = get_conversation_crm_lead(convo)
    if crm_lead:
        return "CRM Lead", crm_lead

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


def _parse_mcp_tool_names(value: str | None) -> set[str]:
    names = {
        part.strip()
        for part in re.split(r"[\s,]+", str(value or ""))
        if part.strip()
    }
    return {
        name
        for name in names
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
    }
