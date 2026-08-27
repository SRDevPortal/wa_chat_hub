from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from wa_chat_hub.security import safe_ai_get_all, safe_ai_get_doc


CHAT_CAPABILITY = "use_for_chat"
VISION_CAPABILITY = "use_for_vision"
TRANSCRIPTION_CAPABILITY = "use_for_transcription"

OPENAI_COMPATIBLE_PROVIDER_TYPES = {"OpenAI", "Gemini", "Custom"}


def get_active_llm_provider_rows(capability: str | None = None, limit: int | None = None) -> list[Any]:
    """Return active LLM provider rows for a task-specific capability.

    If capability fields are present and at least one active row is explicitly checked for
    that capability, the checked rows are authoritative. Otherwise, preserve backward
    compatibility with model/provider-name heuristics.
    """
    meta = frappe.get_meta("WA LLM Provider")
    fields = ["name", "provider_type", "model_name", "base_url"]
    for fieldname in (CHAT_CAPABILITY, VISION_CAPABILITY, TRANSCRIPTION_CAPABILITY):
        if meta.has_field(fieldname):
            fields.append(fieldname)

    query_limit = None if capability else limit
    rows = safe_ai_get_all(
        "WA LLM Provider",
        filters={"is_active": 1},
        fields=fields,
        order_by="priority asc, modified desc",
        limit=query_limit,
    )
    rows = [row for row in rows if row.provider_type in OPENAI_COMPATIBLE_PROVIDER_TYPES]
    if not capability:
        return rows

    if meta.has_field(capability):
        explicit = [row for row in rows if cint(row.get(capability))]
        if explicit:
            return explicit[:limit] if limit else explicit

    if capability == CHAT_CAPABILITY:
        matches = [row for row in rows if not looks_like_transcription_model(row.model_name, row.base_url)]
        return matches[:limit] if limit else matches
    if capability == VISION_CAPABILITY:
        matches = [row for row in rows if looks_like_vision_model(row.provider_type, row.model_name)]
        return matches[:limit] if limit else matches
    if capability == TRANSCRIPTION_CAPABILITY:
        matches = [
            row
            for row in rows
            if looks_like_transcription_provider(row.provider_type, row.model_name, row.base_url)
        ]
        return matches[:limit] if limit else matches
    return rows


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
