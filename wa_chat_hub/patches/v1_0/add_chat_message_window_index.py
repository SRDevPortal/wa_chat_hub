from __future__ import annotations

import frappe


INDEX_NAME = "idx_chat_message_conversation_direction_creation"


def execute() -> None:
    if not frappe.db.exists("DocType", "Chat Message"):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        frappe.db.add_index(
            "Chat Message",
            ["conversation", "direction", "creation"],
            index_name=INDEX_NAME,
        )
    finally:
        frappe.flags.in_migrate = previous
