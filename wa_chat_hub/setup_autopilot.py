"""One-time / repeatable setup for WA Chat Hub AI autopilot."""

import frappe

DEFAULT_SYSTEM_PROMPT = """You are a real WhatsApp care coordinator for SRIAAS.
Answer the customer's latest message naturally. Keep replies short for WhatsApp.
Do not diagnose or prescribe."""

DEFAULT_GUARDRAILS = """Never diagnose or prescribe. Do not claim to be a doctor.
For urgent symptoms, advise a qualified clinician. Keep each reply short and conversational."""

DEFAULT_ESCALATION = """If the user asks for a human agent, appointment booking you cannot complete, or expresses anger, \
reply politely and say a team member will follow up shortly."""


def configure_autopilot_settings():
    settings = frappe.get_single("WA Chat Hub Settings")
    settings.enable_ai_autopilot = 1
    settings.enable_ai_summary = 1
    settings.enable_ai_reply_draft = 1
    settings.enable_multilingual_replies = 1
    settings.autopilot_mode = "Limited Auto Reply"
    if not settings.system_prompt:
        settings.system_prompt = DEFAULT_SYSTEM_PROMPT
    if not settings.medical_guardrail_policy:
        settings.medical_guardrail_policy = DEFAULT_GUARDRAILS
    if not settings.escalation_policy:
        settings.escalation_policy = DEFAULT_ESCALATION
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    return settings.name


def ensure_default_llm_provider():
    existing = frappe.get_all("WA LLM Provider", filters={"is_active": 1}, pluck="name", limit=1)
    if existing:
        return existing[0]

    api_key = frappe.conf.get("openai_api_key") or frappe.get_site_config().get("openai_api_key")
    if not api_key:
        frappe.throw(
            "No active WA LLM Provider and no openai_api_key in site_config. "
            "Create a WA LLM Provider manually or add openai_api_key to site_config."
        )

    doc = frappe.get_doc(
        {
            "doctype": "WA LLM Provider",
            "title": "OpenAI Default",
            "is_active": 1,
            "priority": 1,
            "provider_type": "OpenAI",
            "model_name": "gpt-4o-mini",
            "api_key": api_key,
            "is_embedding_provider": 1,
        }
    )
    doc.insert(ignore_permissions=True)
    frappe.db.commit()
    return doc.name


def run():
    configure_autopilot_settings()
    provider = ensure_default_llm_provider()
    return {"settings": "WA Chat Hub Settings", "llm_provider": provider}
