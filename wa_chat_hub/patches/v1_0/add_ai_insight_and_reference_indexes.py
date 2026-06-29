from __future__ import annotations

import frappe


def execute() -> None:
    ensure_wa_lead_ai_insight_indexes()
    ensure_sales_invoice_item_reference_indexes()


def ensure_wa_lead_ai_insight_indexes() -> None:
    if not frappe.db.exists("DocType", "WA Lead AI Insight"):
        return

    _add_index(
        "WA Lead AI Insight",
        ["lead", "creation"],
        "idx_wa_lead_ai_insight_lead_creation",
    )
    _add_index(
        "WA Lead AI Insight",
        ["lead", "insight_type", "creation"],
        "idx_wa_lead_ai_insight_lead_type_creation",
    )
    _add_index(
        "WA Lead AI Insight",
        ["conversation", "creation"],
        "idx_wa_lead_ai_insight_conversation_creation",
    )
    _add_index(
        "WA Lead AI Insight",
        ["channel_account", "creation"],
        "idx_wa_lead_ai_insight_channel_creation",
    )
    _add_index(
        "WA Lead AI Insight",
        ["pipeline", "insight_type", "creation"],
        "idx_wa_lead_ai_insight_pipeline_type_creation",
    )


def ensure_sales_invoice_item_reference_indexes() -> None:
    if not frappe.db.exists("DocType", "Sales Invoice Item"):
        return
    if not _has_columns("Sales Invoice Item", ["reference_dt", "reference_dn"]):
        return

    _add_index("Sales Invoice Item", ["reference_dt"], "idx_sii_reference_dt")
    _add_index("Sales Invoice Item", ["reference_dt", "reference_dn"], "idx_sii_reference_dt_dn")
    _add_index("Sales Invoice Item", ["reference_dn"], "idx_sii_reference_dn")


def _add_index(doctype: str, fields: list[str], index_name: str) -> None:
    if not _has_columns(doctype, fields):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        frappe.db.add_index(doctype, fields, index_name=index_name[:64])
    finally:
        frappe.flags.in_migrate = previous


def _has_columns(doctype: str, fields: list[str]) -> bool:
    return all(frappe.db.has_column(doctype, fieldname) for fieldname in fields)
