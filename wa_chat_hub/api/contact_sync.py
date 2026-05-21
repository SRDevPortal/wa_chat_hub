"""Whitelisted APIs to push contacts to Interakt."""

from __future__ import annotations

import frappe
from frappe import _

from wa_chat_hub.interakt.contact_sync import (
    push_contact_to_interakt,
    push_pipeline_map_contacts,
    push_reference_to_interakt,
)


@frappe.whitelist()
def sync_pipeline_map_to_interakt(pipeline_map: str):
    """Bulk push patients, leads, and conversation contacts for one WA Channel Pipeline Map."""
    if not pipeline_map:
        frappe.throw(_("pipeline_map is required"))
    result = push_pipeline_map_contacts(pipeline_map)
    return {"success": True, "result": result}


@frappe.whitelist()
def push_patient_to_interakt(patient: str):
    if not patient or not frappe.db.exists("Patient", patient):
        frappe.throw(_("Patient not found"))
    result = push_reference_to_interakt(frappe.get_doc("Patient", patient))
    return {"success": True, "result": result}


@frappe.whitelist()
def push_crm_lead_to_interakt(lead: str):
    if not lead or not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("CRM Lead not found"))
    result = push_reference_to_interakt(frappe.get_doc("CRM Lead", lead))
    return {"success": True, "result": result}


@frappe.whitelist()
def push_chat_contact_to_interakt(contact: str, channel_account: str):
    if not contact or not channel_account:
        frappe.throw(_("contact and channel_account are required"))
    result = push_contact_to_interakt(channel_account, contact)
    return {"success": True, "result": result}
