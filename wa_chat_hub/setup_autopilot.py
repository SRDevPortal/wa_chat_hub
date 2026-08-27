"""One-time / repeatable setup for WA Chat Hub AI autopilot."""

import frappe

DEFAULT_SYSTEM_PROMPT = """You are ShipKia's WhatsApp sales and support assistant.
Speak like a warm, senior Indian shipping sales agent in the customer's language.

Core behavior:
- Answer the customer's latest question first.
- Ask only one question in one reply, and ask for only one missing detail at a time.
- Do not ask business name, monthly shipments, current provider, pickup city, delivery city, and weight together.
- Treat customer-provided rates, zones, RTO percentage, courier/provider names, and shipment counts as useful facts.
- For general ShipKia questions, always mention order confirmation and NDR workflows along with shipping, courier options, COD, tracking, and RTO support where relevant.

Rate behavior:
- Use ShipKia's approved rate card context when it is supplied by the system.
- Do not invent numerical rates.
- Share only starting/base rates on WhatsApp.
- If flat or flat zonal rates are requested, answer from the approved rate card context.
- If an exact city/pincode/serviceability quote is needed and approved zone/rate context is not available, ask only the next missing detail.
- Mention that final live rates can vary by exact pincode, courier serviceability, taxes, dimensions, and chargeable weight."""

DEFAULT_GUARDRAILS = """Never diagnose or prescribe. Do not claim to be a doctor.
For urgent symptoms, advise a qualified clinician. Keep each reply short and conversational."""

DEFAULT_ESCALATION = """If the user asks for a human agent, appointment booking you cannot complete, or expresses anger, \
reply politely and say a team member will follow up shortly."""

BUOPSO_VLLM_PROVIDER_TITLE = "Buopso vLLM Qwen"
BUOPSO_VLLM_CHAT_COMPLETIONS_URL = "https://vllm.buopso.net/v1/chat/completions"
BUOPSO_VLLM_MODEL = "qwen3:4b"

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


def ensure_buopso_vllm_provider(api_key: str | None = None):
    api_key = (
        api_key
        or frappe.conf.get("wa_buopso_vllm_api_key")
        or frappe.conf.get("vllm_buopso_api_key")
        or frappe.conf.get("wa_chat_hub_vllm_api_key")
    )
    if not api_key:
        frappe.throw(
            "Buopso vLLM API key is required. Pass api_key or set wa_buopso_vllm_api_key in site_config."
        )

    existing = frappe.db.exists("WA LLM Provider", BUOPSO_VLLM_PROVIDER_TITLE)
    doc = frappe.get_doc("WA LLM Provider", existing) if existing else frappe.new_doc("WA LLM Provider")
    doc.update(
        {
            "title": BUOPSO_VLLM_PROVIDER_TITLE,
            "is_active": 1,
            "priority": 1,
            "provider_type": "Custom",
            "model_name": BUOPSO_VLLM_MODEL,
            "base_url": BUOPSO_VLLM_CHAT_COMPLETIONS_URL,
            "use_for_chat": 1,
            "use_for_vision": 0,
            "use_for_transcription": 0,
            "is_embedding_provider": 0,
        }
    )
    doc.api_key = api_key
    if doc.is_new():
        doc.insert(ignore_permissions=True)
    else:
        doc.save(ignore_permissions=True)
    frappe.db.commit()
    return doc.name



def run():
    configure_autopilot_settings()
    provider = ensure_default_llm_provider()
    return {"settings": "WA Chat Hub Settings", "llm_provider": provider}
