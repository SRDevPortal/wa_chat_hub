from __future__ import annotations

import frappe
from frappe import _

from wa_chat_hub.settings import get_active_knowledge_base


@frappe.whitelist()
def get_autopilot_status():
    settings = frappe.get_single("WA Chat Hub Settings")
    enabled = bool(settings.enable_ai_autopilot)
    mode = settings.autopilot_mode or "Suggest Only"
    return {
        "success": True,
        "enabled": enabled,
        "mode": mode,
        "sends_whatsapp": enabled and mode == "Limited Auto Reply",
    }


@frappe.whitelist()
def set_autopilot_enabled(enabled=0):
    if not frappe.has_permission("WA Chat Hub Settings", "write"):
        frappe.throw(_("Not permitted to change WA Chat Hub Settings"), frappe.PermissionError)

    settings = frappe.get_single("WA Chat Hub Settings")
    turn_on = int(enabled) == 1
    settings.enable_ai_autopilot = 1 if turn_on else 0

    if turn_on:
        # UI toggle means "reply on WhatsApp", not draft-only.
        settings.autopilot_mode = "Limited Auto Reply"

    settings.save(ignore_permissions=True)
    frappe.db.commit()

    mode = settings.autopilot_mode or "Suggest Only"
    return {
        "success": True,
        "enabled": bool(settings.enable_ai_autopilot),
        "mode": mode,
        "sends_whatsapp": bool(settings.enable_ai_autopilot) and mode == "Limited Auto Reply",
    }


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
