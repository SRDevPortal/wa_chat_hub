"""Resolve Interakt Chat Channel Account from WA Channel Pipeline Map."""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _


PIPELINE_MAP_FIELDS = [
    "name",
    "chat_channel_account",
    "sr_lead_pipeline",
    "sr_medical_department",
    "is_active",
]


def get_pipeline_map(
    *,
    pipeline: Optional[str] = None,
    medical_department: Optional[str] = None,
    channel_account: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a single active WA Channel Pipeline Map row."""
    if not frappe.db.exists("DocType", "WA Channel Pipeline Map"):
        frappe.throw(_("WA Channel Pipeline Map is not installed."))

    filters: Dict[str, Any] = {"is_active": 1}
    if channel_account:
        filters["chat_channel_account"] = channel_account
    if pipeline:
        filters["sr_lead_pipeline"] = pipeline
    if medical_department:
        filters["sr_medical_department"] = medical_department

    rows = frappe.get_all(
        "WA Channel Pipeline Map",
        filters=filters,
        fields=PIPELINE_MAP_FIELDS,
        limit_page_length=2,
    )
    if not rows:
        hint = _missing_map_hint(pipeline=pipeline, medical_department=medical_department)
        frappe.throw(hint)
    if len(rows) > 1:
        frappe.throw(_("Multiple active WA Channel Pipeline Map records match. Use one row per Interakt account."))

    row = rows[0]
    _validate_channel_account(row["chat_channel_account"])
    return row


def get_channel_account_for_lead(lead) -> str:
    pipeline = lead.get("sr_lead_pipeline")
    if not pipeline:
        frappe.throw(_("CRM Lead {0} does not have a pipeline (sr_lead_pipeline).").format(lead.name))
    return get_pipeline_map(pipeline=pipeline)["chat_channel_account"]


def get_channel_account_for_patient(patient) -> str:
    department = patient.get("sr_medical_department")
    if not department:
        frappe.throw(_("Patient {0} has no Medical Department (sr_medical_department).").format(patient.name))
    return get_pipeline_map(medical_department=department)["chat_channel_account"]


def get_pipeline_for_channel_account(channel_account: Optional[str]) -> Optional[str]:
    if not channel_account:
        return None
    return frappe.db.get_value(
        "WA Channel Pipeline Map",
        {"chat_channel_account": channel_account, "is_active": 1},
        "sr_lead_pipeline",
    )


def _validate_channel_account(channel_account: str) -> None:
    account = frappe.get_cached_doc("Chat Channel Account", channel_account)
    if not account.is_active:
        frappe.throw(_("Mapped WhatsApp channel {0} is not active.").format(channel_account))
    if account.channel_type != "Interakt":
        frappe.throw(_("Mapped WhatsApp channel {0} must be an Interakt account.").format(channel_account))


def _missing_map_hint(*, pipeline: Optional[str], medical_department: Optional[str]) -> str:
    if medical_department:
        return _("No active WA Channel Pipeline Map for Medical Department {0}.").format(medical_department)
    if pipeline:
        return _("No active WA Channel Pipeline Map for SR Lead Pipeline {0}.").format(pipeline)
    return _("No active WA Channel Pipeline Map found.")
