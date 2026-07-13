from __future__ import annotations

import frappe


def execute() -> None:
    if not frappe.db.exists("DocType", "WA Conversation Audit Event"):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        frappe.db.add_index(
            "WA Conversation Audit Event",
            ["conversation", "creation"],
            index_name="idx_wa_conv_audit_conversation_creation",
        )
        frappe.db.add_index(
            "WA Conversation Audit Event",
            ["event_type", "creation"],
            index_name="idx_wa_conv_audit_type_creation",
        )
        frappe.db.add_index(
            "WA Conversation Audit Event",
            ["source_message"],
            index_name="idx_wa_conv_audit_source_message",
        )
    finally:
        frappe.flags.in_migrate = previous
