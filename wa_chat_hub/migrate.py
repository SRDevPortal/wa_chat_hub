from __future__ import annotations

from pathlib import Path

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


MODULE = "wa_chat_hub"


def after_migrate() -> None:
    """Keep WA Chat Hub standard doctypes and workspace synced after migrate."""
    sync_standard_doctypes()
    _sync_chat_conversation_schema()
    try:
        from wa_chat_hub.setup_workspace import run as setup_workspace

        setup_workspace()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Workspace Sync Failed")
    ensure_lead_scoring_fields()
    ensure_chat_message_indexes()
    migrate_conversation_crm_lead_links()
    backfill_messaging_windows()


def _sync_chat_conversation_schema() -> None:
    """Ensure messaging window columns exist on tabChat Conversation."""
    try:
        frappe.reload_doc("wa_chat_hub", "doctype", "Chat Conversation", force=True)
        frappe.clear_cache(doctype="Chat Conversation")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Chat Conversation Schema Sync Failed")


def backfill_messaging_windows() -> None:
    try:
        from wa_chat_hub.messaging.windows import (
            backfill_messaging_windows_from_history,
            repair_ctwa_false_positives,
        )

        repair_ctwa_false_positives()
        backfill_messaging_windows_from_history()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Messaging Window Backfill Failed")


def sync_standard_doctypes() -> None:
    doctype_dir = Path(__file__).parent / "wa_chat_hub" / "doctype"
    if not doctype_dir.exists():
        return

    for json_file in sorted(doctype_dir.glob("*/*.json")):
        doctype_name = json_file.parent.name
        try:
            frappe.reload_doc(MODULE, "doctype", doctype_name, force=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"WA Chat Hub DocType Sync Failed: {doctype_name}",
            )


def ensure_lead_scoring_fields() -> None:
    specs = {
        "Lead": "source",
        "CRM Lead": "status",
    }
    custom_fields = {}
    for doctype, insert_after in specs.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        custom_fields[doctype] = [
            {
                "fieldname": "lead_score",
                "label": "lead_score",
                "fieldtype": "Float",
                "insert_after": insert_after,
                "in_list_view": 1,
                "in_standard_filter": 1,
                "default": "0",
                "precision": "2",
            },
            {
                "fieldname": "lead_lan",
                "label": "lead_lan",
                "fieldtype": "Data",
                "insert_after": "lead_score",
                "in_list_view": 1,
                "in_standard_filter": 1,
            },
            {
                "fieldname": "lead_temperature",
                "label": "Lead_temperature",
                "fieldtype": "Select",
                "insert_after": "lead_lan",
                "in_list_view": 1,
                "in_standard_filter": 1,
                "options": "Cold\nWarm\nHot",
                "default": "Cold",
            },
        ]
    if custom_fields:
        create_custom_fields(custom_fields, update=True)


def ensure_chat_message_indexes() -> None:
    try:
        from wa_chat_hub.patches.v1_0.add_chat_message_indexes import (
            ensure_chat_conversation_indexes,
            ensure_chat_message_indexes,
            ensure_crm_lead_indexes,
        )

        ensure_chat_message_indexes()
        ensure_chat_conversation_indexes()
        ensure_crm_lead_indexes()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Chat Message Index Sync Failed")


def migrate_conversation_crm_lead_links() -> None:
    """Copy legacy CRM Lead / Lead links into linked_crm_lead Link field."""
    if not frappe.db.exists("DocType", "Chat Conversation"):
        return
    meta = frappe.get_meta("Chat Conversation")
    if not meta.has_field("linked_crm_lead"):
        return

    if not frappe.db.exists("DocType", "CRM Lead"):
        return

    frappe.db.sql(
        """
        UPDATE `tabChat Conversation` c
        INNER JOIN `tabCRM Lead` l ON l.name = c.linked_reference_name
        SET c.linked_crm_lead = c.linked_reference_name,
            c.linked_reference_doctype = 'CRM Lead'
        WHERE IFNULL(c.linked_reference_doctype, '') IN ('CRM Lead', 'Lead')
          AND IFNULL(c.linked_reference_name, '') != ''
          AND IFNULL(c.linked_crm_lead, '') = ''
        """
    )
