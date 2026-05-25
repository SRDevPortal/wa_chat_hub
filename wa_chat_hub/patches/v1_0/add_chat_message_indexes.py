from __future__ import annotations

import frappe


def execute() -> None:
    ensure_chat_message_indexes()


def ensure_chat_message_indexes() -> None:
    if not frappe.db.exists("DocType", "Chat Message"):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        frappe.db.add_index(
            "Chat Message",
            ["conversation", "creation"],
            index_name="idx_chat_message_conversation_creation",
        )
        frappe.db.add_index(
            "Chat Message",
            ["channel_message_id"],
            index_name="idx_chat_message_channel_message_id",
        )
        frappe.db.add_index(
            "Chat Message",
            ["provider_message_id"],
            index_name="idx_chat_message_provider_message_id",
        )
    finally:
        frappe.flags.in_migrate = previous
