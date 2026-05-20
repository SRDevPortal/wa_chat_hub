from __future__ import annotations

import frappe

from wa_chat_hub.settings import get_active_knowledge_base


@frappe.whitelist()
def get_ai_settings_context():
    settings = frappe.get_single("WA Chat Hub Settings") if frappe.db.exists("DocType", "WA Chat Hub Settings") else None
    return {
        "success": True,
        "result": {
            "settings": settings.as_dict() if settings else {},
            "knowledge_base": get_active_knowledge_base(),
            "channel_contexts": frappe.get_all(
                "WA Channel Context",
                filters={"is_active": 1},
                fields=["name", "context_name", "pipeline", "channel_account", "department"],
                order_by="modified desc",
            ),
        },
    }
