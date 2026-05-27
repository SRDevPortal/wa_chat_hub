from __future__ import annotations

import frappe


def execute() -> None:
    ensure_chat_message_indexes()
    ensure_chat_conversation_indexes()
    ensure_crm_lead_indexes()


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
        frappe.db.add_index(
            "Chat Message",
            ["direction", "creation"],
            index_name="idx_chat_message_direction_creation",
        )
    finally:
        frappe.flags.in_migrate = previous


def ensure_chat_conversation_indexes() -> None:
    if not frappe.db.exists("DocType", "Chat Conversation"):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        frappe.db.add_index(
            "Chat Conversation",
            ["status", "unread_count"],
            index_name="idx_chat_conversation_status_unread",
        )
        frappe.db.add_index(
            "Chat Conversation",
            ["contact", "status"],
            index_name="idx_chat_conversation_contact_status",
        )
        if frappe.db.has_column("Chat Conversation", "linked_crm_lead"):
            frappe.db.add_index(
                "Chat Conversation",
                ["linked_crm_lead", "modified"],
                index_name="idx_chat_conversation_linked_crm_lead",
            )
            frappe.db.add_index(
                "Chat Conversation",
                ["contact", "linked_crm_lead"],
                index_name="idx_chat_conversation_contact_crm_lead",
            )
        if frappe.db.has_column("Chat Conversation", "linked_reference_doctype") and frappe.db.has_column(
            "Chat Conversation", "linked_reference_name"
        ):
            frappe.db.add_index(
                "Chat Conversation",
                ["linked_reference_doctype", "linked_reference_name", "modified"],
                index_name="idx_chat_conversation_reference",
            )
            frappe.db.add_index(
                "Chat Conversation",
                ["contact", "linked_reference_doctype", "linked_reference_name"],
                index_name="idx_chat_conversation_contact_reference",
            )
    finally:
        frappe.flags.in_migrate = previous


def ensure_crm_lead_indexes() -> None:
    if not frappe.db.exists("DocType", "CRM Lead"):
        return
    if not frappe.db.has_column("CRM Lead", "sr_duplicate_of_name"):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        frappe.db.add_index(
            "CRM Lead",
            ["sr_duplicate_of_name"],
            index_name="idx_crmlead_duplicate_of_name",
        )
    finally:
        frappe.flags.in_migrate = previous
