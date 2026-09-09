"""Compatibility for old queued jobs: only maintain ERPNext Lead links."""
import frappe
from wa_chat_hub.security import set_service_user_context
from wa_chat_hub.services import _link_or_create_master_record

def backfill_whatsapp_lead_pairs():
    set_service_user_context("lead_link_backfill")
    stats = {"checked": 0, "updated": 0, "failed": 0}
    for row in frappe.get_all("Chat Conversation", fields=["name", "contact"], limit_page_length=0):
        stats["checked"] += 1
        contact = frappe.get_doc("Chat Contact", row.contact)
        try:
            _link_or_create_master_record(row.name, contact.name, contact.phone_number, contact.display_name)
            stats["updated"] += 1
        except Exception:
            stats["failed"] += 1
            frappe.log_error(frappe.get_traceback(), "Lead link backfill failed")
    return stats
