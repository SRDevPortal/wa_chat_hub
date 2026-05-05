from __future__ import annotations

import frappe

from wa_chat_hub.connector.official.adapter import OfficialWhatsAppAdapter
from wa_chat_hub.connector.personal.adapter import PersonalWhatsAppAdapter


REGISTRY = {
    "Official WhatsApp": OfficialWhatsAppAdapter(),
    "Personal WhatsApp": PersonalWhatsAppAdapter(),
}


def get_adapter(channel_type: str):
    adapter = REGISTRY.get(channel_type)
    if adapter is None:
        frappe.throw(
            f"Unsupported channel type: '{channel_type}'. "
            f"Valid options are: {', '.join(REGISTRY.keys())}",
            title="Unknown Channel Type",
        )
    return adapter
