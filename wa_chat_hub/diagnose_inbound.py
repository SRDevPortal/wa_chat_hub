"""Diagnose why an Interakt inbound message is missing in WA Chat Hub."""

from __future__ import annotations

import frappe

from wa_chat_hub.services import normalize_phone


def run(phone: str = "+918808635823"):
    normalized = normalize_phone(phone)
    last10 = normalized[-10:] if len(normalized) >= 10 else normalized

    contacts = frappe.get_all(
        "Chat Contact",
        or_filters=[
            ["phone_number", "like", f"%{last10}%"],
            ["phone_number", "=", normalized],
            ["name", "like", f"%{last10}%"],
        ],
        fields=["name", "phone_number", "display_name"],
        limit=10,
    )

    conversations = []
    messages = []
    for contact in contacts:
        convs = frappe.get_all(
            "Chat Conversation",
            filters={"contact": contact.name},
            fields=["name", "channel_account", "modified", "last_message_preview", "unread_count"],
            order_by="modified desc",
            limit=5,
        )
        conversations.extend(convs)
        for conv in convs:
            messages.extend(
                frappe.get_all(
                    "Chat Message",
                    filters={"conversation": conv.name},
                    fields=["name", "body", "creation", "direction", "channel_message_id"],
                    order_by="creation desc",
                    limit=5,
                )
            )

    errors = frappe.get_all(
        "Error Log",
        filters={"creation": [">", "2026-05-19"]},
        or_filters=[
            ["error", "like", "%Interakt%"],
            ["method", "like", "%webhook%"],
            ["error", "like", f"%{last10}%"],
        ],
        fields=["name", "creation", "method", "error"],
        order_by="creation desc",
        limit=15,
    )

    accounts = frappe.get_all(
        "Chat Channel Account",
        filters={"channel_type": "Interakt", "is_active": 1},
        fields=["name", "account_name", "connector_status"],
    )

    result = {
        "searched_phone": phone,
        "normalized_phone": normalized,
        "contacts": contacts,
        "conversations": conversations,
        "recent_messages": messages,
        "interakt_accounts": accounts,
        "recent_interakt_errors": [
            {
                "name": row.name,
                "creation": str(row.creation),
                "method": row.method,
                "error_preview": (row.error or "")[:500],
            }
            for row in errors
        ],
        "webhook_url_hint": (
            "https://<YOUR-PUBLIC-HOST>/api/method/wa_chat_hub.api.webhook.receive_interakt"
            "?channel_account=<Chat Channel Account Name>"
        ),
        "likely_causes_if_empty": [
            "Interakt webhook URL points to production, not this localhost bench",
            "Webhook POST failed (signature, channel_account resolution, missing phone in payload)",
            "Message on a different Interakt WABA than the account in webhook URL",
        ],
    }
    print(frappe.as_json(result, indent=1))
    return result
