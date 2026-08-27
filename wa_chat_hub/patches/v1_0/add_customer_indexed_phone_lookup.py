from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


CUSTOMER_PHONE_FIELDS = (
    {
        "fieldname": "vobiz_normalized_phone",
        "label": "Vobiz Normalized Phone",
        "fieldtype": "Data",
        "insert_after": "mobile_no",
        "hidden": 1,
        "read_only": 1,
        "no_copy": 1,
        "module": "WA Chat Hub",
    },
    {
        "fieldname": "vobiz_mobile_last10",
        "label": "Vobiz Mobile Last 10",
        "fieldtype": "Data",
        "insert_after": "vobiz_normalized_phone",
        "hidden": 1,
        "read_only": 1,
        "no_copy": 1,
        "module": "WA Chat Hub",
    },
)

CUSTOMER_PHONE_INDEXES = (
    (
        "idx_wa_customer_normalized_modified",
        ("vobiz_normalized_phone", "modified"),
    ),
    (
        "idx_wa_customer_mobile_last10_modified",
        ("vobiz_mobile_last10", "modified"),
    ),
)


def execute() -> None:
    ensure_customer_phone_lookup_schema()


def ensure_customer_phone_lookup_schema() -> None:
    """Create the generated Customer phone keys and their lookup indexes."""
    if not frappe.db.exists("DocType", "Customer"):
        return

    create_custom_fields(
        {"Customer": [dict(field) for field in CUSTOMER_PHONE_FIELDS]},
        update=True,
        ignore_validate=True,
    )

    previous = getattr(frappe.flags, "in_migrate", False)
    frappe.flags.in_migrate = True
    try:
        for index_name, columns in CUSTOMER_PHONE_INDEXES:
            if not all(frappe.db.has_column("Customer", column) for column in columns):
                continue
            frappe.db.add_index(
                "Customer",
                list(columns),
                index_name=index_name,
            )
    finally:
        frappe.flags.in_migrate = previous
