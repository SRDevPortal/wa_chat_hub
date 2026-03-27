from __future__ import annotations

from typing import Any, Dict

import frappe

from wa_chat_hub.connector.registry import get_adapter


def build_outbound_message_payload(conversation: str, body: str, content_type: str = "Text") -> Dict[str, Any]:
    convo = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", convo.contact)
    account = frappe.get_doc("Chat Channel Account", convo.channel_account)
    adapter = get_adapter(account.channel_type)

    normalized = {
        "phone_number": contact.phone_number,
        "body": body,
        "content_type": content_type,
        "conversation": conversation,
        "channel_account": convo.channel_account,
    }
    return {
        "channel_type": account.channel_type,
        "channel_account": convo.channel_account,
        "payload": adapter.build_outbound_payload(normalized),
    }
