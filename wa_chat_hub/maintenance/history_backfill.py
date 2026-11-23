"""Explicit Bench-only historical repairs. Preview by default; one batch per call."""
from __future__ import annotations

import frappe

from wa_chat_hub.maintenance.manual_batches import run_batch


TASKS = {
    "crm_lead_meta": ("Chat Conversation", ("status",)),
    "messaging_windows": ("Chat Conversation", ("last_customer_message_at", "ctwa_clid")),
    "crm_lead_links": ("Chat Conversation", ("linked_crm_lead", "linked_reference_name", "linked_reference_doctype")),
    "conversation_identities": ("Chat Conversation", ("linked_patient", "party_type", "identity_status", "linked_crm_lead")),
    "channel_departments": ("Chat Channel Account", ("default_medical_department",)),
}


def run(task, batch_size=100, after_name="", dry_run=True):
    if task not in TASKS:
        raise ValueError("Unknown history task: " + str(task))
    doctype, fields = TASKS[task]
    for field in fields:
        if not frappe.db.has_column(doctype, field):
            raise ValueError(f"Missing required column: {doctype}.{field}")
    filters = {}
    if task == "crm_lead_meta":
        if not frappe.db.has_column("CRM Lead", "sr_w_source_id"):
            raise ValueError("CRM Lead attribution fields are not installed")
        filters["status"] = ["!=", "Closed"]
        if frappe.db.has_column(doctype, "linked_crm_lead"):
            filters["linked_crm_lead"] = ["is", "set"]
        else:
            filters.update(linked_reference_doctype="CRM Lead", linked_reference_name=["is", "set"])
    elif task == "crm_lead_links" and not frappe.db.exists("DocType", "CRM Lead"):
        raise ValueError("CRM Lead is not installed")
    elif task == "channel_departments" and not frappe.db.exists("DocType", "WA Channel Pipeline Map"):
        raise ValueError("WA Channel Pipeline Map is not installed")
    return run_batch(operation="wa_chat_hub:" + task, doctype=doctype,
                     process_row=lambda name: _process(task, name), batch_size=batch_size,
                     after_name=after_name, dry_run=dry_run, filters=filters)


def _process(task, name):
    if task == "crm_lead_meta":
        from wa_chat_hub.messaging.crm_lead_meta import sync_crm_lead_meta_from_conversation
        sync_crm_lead_meta_from_conversation(name, force=False)
    elif task == "messaging_windows":
        from wa_chat_hub.messaging.windows import _backfill_single_conversation
        _backfill_single_conversation(name)
    elif task == "crm_lead_links":
        from wa_chat_hub.migrate import migrate_conversation_crm_lead_links
        migrate_conversation_crm_lead_links(names=(name,))
    elif task == "conversation_identities":
        from wa_chat_hub.setup_agents import backfill_conversation_identities
        backfill_conversation_identities(names=(name,))
    elif task == "channel_departments":
        from wa_chat_hub.migrate import backfill_channel_account_medical_departments
        backfill_channel_account_medical_departments(names=(name,))
