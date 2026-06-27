from __future__ import annotations

import frappe


def execute() -> None:
    ensure_chat_message_indexes()
    ensure_chat_conversation_indexes()
    ensure_chat_contact_indexes()
    ensure_chat_contact_channel_profile_indexes()
    ensure_crm_lead_indexes()
    ensure_reference_phone_indexes()


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
        if frappe.db.has_column("Chat Message", "sender_type"):
            frappe.db.add_index(
                "Chat Message",
                ["direction", "sender_type", "creation"],
                index_name="idx_chat_message_direction_sender_creation",
            )
        frappe.db.add_index(
            "Chat Message",
            ["modified"],
            index_name="idx_chat_message_modified",
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
        frappe.db.add_index(
            "Chat Conversation",
            ["channel_account", "contact", "status"],
            index_name="idx_chat_conversation_channel_contact_status",
        )
        frappe.db.add_index(
            "Chat Conversation",
            ["channel_account", "contact", "modified"],
            index_name="idx_chat_conversation_channel_contact_modified",
        )
        frappe.db.add_index(
            "Chat Conversation",
            ["modified"],
            index_name="idx_chat_conversation_modified",
        )
        if frappe.db.has_column("Chat Conversation", "linked_crm_lead"):
            frappe.db.add_index(
                "Chat Conversation",
                ["linked_crm_lead", "modified"],
                index_name="idx_chat_conversation_linked_crm_lead",
            )
            frappe.db.add_index(
                "Chat Conversation",
                ["status", "linked_crm_lead", "unread_count"],
                index_name="idx_chat_conversation_status_crm_unread",
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
                ["status", "linked_reference_doctype", "linked_reference_name", "unread_count"],
                index_name="idx_chat_conversation_status_ref_unread",
            )
            frappe.db.add_index(
                "Chat Conversation",
                ["contact", "linked_reference_doctype", "linked_reference_name"],
                index_name="idx_chat_conversation_contact_reference",
            )
    finally:
        frappe.flags.in_migrate = previous


def ensure_chat_contact_indexes() -> None:
    if not frappe.db.exists("DocType", "Chat Contact"):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        frappe.db.add_index(
            "Chat Contact",
            ["modified"],
            index_name="idx_chat_contact_modified",
        )
        frappe.db.add_index(
            "Chat Contact",
            ["source_doctype", "source_name"],
            index_name="idx_chat_contact_source",
        )
        frappe.db.add_index(
            "Chat Contact",
            ["phone_number"],
            index_name="idx_chat_contact_phone_number",
        )
    finally:
        frappe.flags.in_migrate = previous


def ensure_chat_contact_channel_profile_indexes() -> None:
    if not frappe.db.exists("DocType", "Chat Contact Channel Profile"):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        frappe.db.add_index(
            "Chat Contact Channel Profile",
            ["contact", "channel_account"],
            index_name="idx_chat_profile_contact_channel",
        )
    finally:
        frappe.flags.in_migrate = previous


def ensure_crm_lead_indexes() -> None:
    if not frappe.db.exists("DocType", "CRM Lead"):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        if frappe.db.has_column("CRM Lead", "sr_duplicate_of_name"):
            frappe.db.add_index(
                "CRM Lead",
                ["sr_duplicate_of_name"],
                index_name="idx_crmlead_duplicate_of_name",
            )
        if frappe.db.has_column("CRM Lead", "lead_owner") and frappe.db.has_column(
            "CRM Lead", "sr_lead_pipeline"
        ):
            frappe.db.add_index(
                "CRM Lead",
                ["lead_owner", "sr_lead_pipeline"],
                index_name="idx_crmlead_owner_pipeline",
            )
    finally:
        frappe.flags.in_migrate = previous


def ensure_reference_phone_indexes() -> None:
    for doctype, fields in {
        "CRM Lead": ("mobile_no", "phone", "mobile", "custom_whatsapp_number"),
        "Lead": ("mobile_no", "phone", "mobile", "custom_whatsapp_number"),
        "Patient": ("mobile", "mobile_no", "phone", "custom_whatsapp_number"),
        "Customer": ("mobile_no", "phone", "custom_whatsapp_number"),
    }.items():
        _ensure_phone_indexes_for_doctype(doctype, fields)


def _ensure_phone_indexes_for_doctype(doctype: str, fields: tuple[str, ...]) -> None:
    if not frappe.db.exists("DocType", doctype):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        for fieldname in fields:
            if not frappe.db.has_column(doctype, fieldname):
                continue
            index_name = f"idx_wa_{doctype.lower().replace(' ', '_')}_{fieldname}"
            frappe.db.add_index(doctype, [fieldname], index_name=index_name[:64])
    finally:
        frappe.flags.in_migrate = previous
