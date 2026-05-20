from __future__ import annotations

import frappe


def get_or_create_settings():
    name = frappe.db.get_single_value("WA Chat Hub Settings", "name")
    if not name:
        doc = frappe.get_doc({"doctype": "WA Chat Hub Settings"})
        doc.insert(ignore_permissions=True)
        return doc
    return frappe.get_single("WA Chat Hub Settings")


def get_active_mcp_servers():
    return []


def get_active_knowledge_base(context: str | None = None, department: str | None = None):
    if context:
        doc = frappe.get_doc("WA Channel Context", context)
        names = [row.knowledge_base for row in doc.get("knowledge_bases") or [] if row.is_active and row.knowledge_base]
        if not names:
            return []
        return frappe.get_all(
            "WA AI Knowledge Base",
            filters={"name": ["in", names], "is_active": 1},
            fields=["name", "kb_label", "kb_type", "source_path", "source_url", "department", "priority", "content"],
            order_by="priority desc, modified desc",
        )

    filters = {"is_active": 1}
    if department:
        filters["department"] = ["in", ["", department]]
    return frappe.get_all(
        "WA AI Knowledge Base",
        filters=filters,
        fields=["name", "kb_label", "kb_type", "source_path", "source_url", "department", "priority", "content"],
        order_by="priority desc, modified desc",
    )
