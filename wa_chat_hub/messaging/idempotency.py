from __future__ import annotations

import hashlib

import frappe
from frappe.utils import cint


def webhook_idempotency_enabled() -> bool:
    """Return the rollout flag, defaulting safely to enabled during deployment."""
    try:
        settings = frappe.get_cached_doc("WA Chat Hub Settings")
        if not settings.meta.has_field("enable_webhook_idempotency"):
            return True
        return bool(cint(settings.enable_webhook_idempotency))
    except Exception:
        # Code can be deployed before migrate creates the settings field.
        return True


def build_message_dedupe_key(
    *,
    conversation: str | None,
    provider_name: str | None,
    provider_message_id: str | None,
    channel_message_id: str | None,
    provider_event_id: str | None = None,
) -> str | None:
    """Build a compact stable key; return None when the provider supplied no identity."""
    external_id = str(
        provider_message_id or channel_message_id or provider_event_id or ""
    ).strip()
    conversation = str(conversation or "").strip()
    if not external_id or not conversation:
        return None
    raw = f"{str(provider_name or 'unknown').strip().lower()}:{conversation}:{external_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
