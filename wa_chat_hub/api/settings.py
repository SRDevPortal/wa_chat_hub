from __future__ import annotations

import frappe

from wa_chat_hub.settings import get_active_knowledge_base, get_active_mcp_servers


@frappe.whitelist()
def get_ai_settings_context():
    settings = frappe.get_single("WA Chat Hub Settings") if frappe.db.exists("DocType", "WA Chat Hub Settings") else None
    return {
        "success": True,
        "result": {
            "settings": settings.as_dict() if settings else {},
            "knowledge_base": get_active_knowledge_base(),
            "mcp_servers": get_active_mcp_servers(),
            "tool_permissions": frappe.get_all(
                "WA AI Tool Permission",
                filters={"is_active": 1},
                fields=["name", "tool_name", "scope", "department", "allow_read", "allow_write", "allow_execute"],
                order_by="modified desc",
            ),
        },
    }
