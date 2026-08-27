"""Read Interakt settings from Chat Channel Account (single source of truth)."""

from __future__ import annotations

from typing import Any, Dict

import frappe
from frappe import _


def get_interakt_account(channel_account: str):
    if not channel_account:
        frappe.throw(_("channel_account is required"))
    account = frappe.get_cached_doc("Chat Channel Account", channel_account)
    if account.channel_type != "Interakt":
        frappe.throw(_("{0} is not an Interakt channel account.").format(channel_account))
    if not account.is_active:
        frappe.throw(_("Chat Channel Account {0} is not active.").format(channel_account))
    return account


def account_routing_context(account) -> Dict[str, Any]:
    """Values from Chat Channel Account form — used by webhook + outbound."""
    return {
        "channel_account": account.name,
        "channel_department": getattr(account, "department", None) or None,
        "default_country_code": getattr(account, "interakt_default_country_code", None) or "+91",
        "default_language_code": getattr(account, "interakt_default_language_code", None) or "en",
        "message_api_url": getattr(account, "interakt_base_url", None) or "https://api.interakt.ai/v1/public/message/",
        "organization_id": getattr(account, "interakt_organization_id", None),
        "templates_api_url": getattr(account, "interakt_templates_api_url", None),
        "phone_number": getattr(account, "phone_number", None),
        "verify_webhook_signature": bool(getattr(account, "interakt_webhook_verify_signature", 1)),
    }


def should_verify_webhook_signature(account) -> bool:
    return bool(getattr(account, "interakt_webhook_verify_signature", 1))


def interakt_api_root(account) -> str:
    """Derive https://api.interakt.ai/v1 from account message API URL."""
    message_url = (getattr(account, "interakt_base_url", None) or "").strip()
    if message_url:
        for marker in ("/public/message/", "/public/message"):
            if marker in message_url:
                return message_url.split(marker)[0].rstrip("/")
        if "/v1/" in message_url:
            return message_url.split("/v1/")[0] + "/v1"
    return "https://api.interakt.ai/v1"


def track_users_api_url(account) -> str:
    return f"{interakt_api_root(account)}/public/track/users/"


def media_upload_api_url(account) -> str:
    return f"{interakt_api_root(account)}/public/track/files/upload_to_fb/"
