"""Interakt User Track API — push contacts to Interakt."""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
import requests
from frappe import _


INTERAKT_TRACK_USER_URL = "https://api.interakt.ai/v1/public/track/users/"


def get_interakt_api_key(channel_account: str) -> str:
    account = frappe.get_doc("Chat Channel Account", channel_account)
    if account.channel_type != "Interakt":
        frappe.throw(_("Channel Account {0} is not an Interakt account.").format(channel_account))
    api_key = account.get_password("interakt_api_key")
    if not api_key:
        frappe.throw(_("Interakt API Key is not configured for {0}.").format(channel_account))
    return api_key


def track_user(channel_account: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POST /v1/public/track/users/ — create or update Interakt user."""
    api_key = get_interakt_api_key(channel_account)
    response = requests.post(
        INTERAKT_TRACK_USER_URL,
        headers={
            "Authorization": f"Basic {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    raw = response.text
    if not response.ok:
        frappe.log_error(
            f"Interakt Track User {response.status_code}: {raw}\nPayload: {frappe.as_json(payload)}",
            "Interakt Track User Failed",
        )
        response.raise_for_status()

    return response.json() if response.content else {}


def extract_interakt_user_id(result: Dict[str, Any]) -> Optional[str]:
    for source in (
        result,
        result.get("data") if isinstance(result, dict) else None,
        result.get("result") if isinstance(result, dict) else None,
    ):
        if isinstance(source, dict):
            value = source.get("userId") or source.get("user_id") or source.get("id")
            if value:
                return str(value)
    return None
