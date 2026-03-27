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
    return frappe.get_all(
        "WA MCP Server",
        filters={"is_active": 1},
        fields=["name", "server_label", "transport", "server_url", "allowed_tools", "auth_type"],
        order_by="modified desc",
    )


def get_active_knowledge_base():
    return frappe.get_all(
        "WA AI Knowledge Base",
        filters={"is_active": 1},
        fields=["name", "kb_label", "kb_type", "source_path", "source_url", "department", "priority"],
        order_by="priority desc, modified desc",
    )
