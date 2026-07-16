from __future__ import annotations

import frappe


def before_tests() -> None:
    """Create framework test principals required by linked DocType fixtures."""
    for index, email in enumerate(
        (
            "test@example.com",
            "test1@example.com",
            "test2@example.com",
            "test3@example.com",
            "test4@example.com",
        )
    ):
        if frappe.db.exists("User", email):
            continue
        frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": f"_Test{index or ''}",
                "enabled": 1,
                "send_welcome_email": 0,
            }
        ).insert(ignore_permissions=True)
    frappe.db.commit()
