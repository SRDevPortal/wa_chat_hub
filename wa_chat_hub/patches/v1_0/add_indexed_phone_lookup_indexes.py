from __future__ import annotations

import frappe


PHONE_INDEX_FIELDS = (
    "vobiz_normalized_phone",
    "vobiz_mobile_last10",
    "vobiz_phone_last10",
    "vobiz_whatsapp_last10",
    "sr_mobile_norm",
)


def execute() -> None:
    for doctype in ("Lead", "Lead", "Patient", "Customer"):
        _ensure_indexes(doctype)


def _ensure_indexes(doctype: str) -> None:
    if not frappe.db.exists("DocType", doctype):
        return

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        for fieldname in PHONE_INDEX_FIELDS:
            if not frappe.db.has_column(doctype, fieldname):
                continue
            if frappe.db.get_column_index(f"tab{doctype}", fieldname, unique=False):
                continue
            slug = doctype.lower().replace(" ", "_")
            frappe.db.add_index(
                doctype,
                [fieldname],
                index_name=f"idx_wa_{slug}_{fieldname}"[:64],
            )
    finally:
        frappe.flags.in_migrate = previous
