import frappe

from wa_chat_hub.messaging.crm_lead_meta import backfill_all_linked_leads


def execute():
    if not frappe.db.exists("DocType", "Lead"):
        return
    if not frappe.get_meta("Lead").has_field("sr_w_source_id"):
        return
    backfill_all_linked_leads()
