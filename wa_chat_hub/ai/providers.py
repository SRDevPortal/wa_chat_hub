from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from wa_chat_hub.security import safe_ai_get_all, safe_ai_get_doc


CHAT_CAPABILITY = "use_for_chat"
VISION_CAPABILITY = "use_for_vision"
TRANSCRIPTION_CAPABILITY = "use_for_transcription"

OPENAI_COMPATIBLE_PROVIDER_TYPES = {"OpenAI", "Gemini", "Custom"}


MEDIA_PROVIDER_FIELDS = ("transcription_mode", "transcription_model", "audio_input_format", "analyse_video_frames")


def get_active_llm_provider_rows(capability: str | None = None, limit: int | None = None,
                                 channel_account: str | None = None, *, optional_capability: bool = False) -> list[Any]:
    """Channel choice is exclusive; otherwise use active providers in priority order."""
    selected = None
    if channel_account and frappe.get_meta("Chat Channel Account").has_field("llm_provider"):
        selected = safe_ai_get_doc("Chat Channel Account", channel_account).get("llm_provider")
    meta = frappe.get_meta("WA LLM Provider")
    fields = ["name", "provider_type", "model_name", "base_url"]
    fields += [f for f in (CHAT_CAPABILITY, VISION_CAPABILITY, TRANSCRIPTION_CAPABILITY, *MEDIA_PROVIDER_FIELDS) if meta.has_field(f)]
    filters = {"is_active": 1}
    if selected:
        filters["name"] = selected
    rows = safe_ai_get_all("WA LLM Provider", filters=filters, fields=fields,
                           order_by="priority asc, modified desc", limit=0)
    rows = [row for row in rows if row.provider_type in OPENAI_COMPATIBLE_PROVIDER_TYPES]
    if selected and not rows:
        frappe.throw(f"Channel {channel_account}: selected LLM Provider {selected} must be active and OpenAI-compatible.")
    if capability:
        rows = [row for row in rows if cint(row.get(capability))]
        if selected and not rows and not optional_capability:
            frappe.throw(f"Channel {channel_account}: selected LLM Provider {selected} does not enable {capability}.")
    return rows[:limit] if limit else rows


def get_provider_secret(row: Any) -> dict[str, Any] | None:
    doc = safe_ai_get_doc("WA LLM Provider", row.name)
    api_key = doc.get_password("api_key", raise_exception=False)
    if not api_key and provider_requires_api_key(row.base_url):
        return None
    return {
        "name": row.name,
        "provider_type": row.provider_type,
        "model_name": row.model_name,
        "base_url": row.base_url,
        "api_key": api_key or "",
        **{field: doc.get(field) for field in MEDIA_PROVIDER_FIELDS},
    }


def provider_requires_api_key(base_url: str | None) -> bool:
    return True


def looks_like_vision_model(provider_type: str, model_name: str | None) -> bool:
    provider_type = str(provider_type or "").lower()
    model_name = str(model_name or "").lower()
    if provider_type == "gemini":
        return True
    vision_tokens = (
        "gpt-4o",
        "gpt-4.1",
        "gpt-4.5",
        "o3",
        "o4",
        "vision",
        "gemini",
        "gemma-4",
        "llava",
        "pixtral",
        "qwen-vl",
        "vl",
        "omni",
        "nex-n2",
        "image",
        "video",
        "ocr",
    )
    text_only_tokens = ("gpt-3.5", "text-", "embedding", "babbage", "davinci", "whisper")
    return any(token in model_name for token in vision_tokens) and not any(
        token in model_name for token in text_only_tokens
    )


def looks_like_transcription_model(model_name: str | None, base_url: str | None = None) -> bool:
    model = str(model_name or "").strip().lower()
    url = str(base_url or "").strip().lower()
    return "whisper" in model or "transcribe" in model or url.endswith("/audio/transcriptions")


def looks_like_transcription_provider(
    provider_type: str,
    model_name: str | None,
    base_url: str | None = None,
) -> bool:
    if looks_like_transcription_model(model_name, base_url):
        return True
    # Backward-compatible OpenAI account: chat model name may be configured, but the
    # transcription code sends whisper-1 to the OpenAI audio endpoint.
    return str(provider_type or "") == "OpenAI"
