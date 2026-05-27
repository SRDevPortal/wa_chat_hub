from __future__ import annotations

import frappe
from frappe import _

from wa_chat_hub.ai.service import generate_reply_draft, generate_summary
from wa_chat_hub.permissions import ensure_can_read_conversation


@frappe.whitelist()
def summarize_conversation(conversation):
    if not conversation:
        frappe.throw(_("conversation is required"))
    ensure_can_read_conversation(conversation)
    return {"success": True, "result": generate_summary(conversation)}


@frappe.whitelist()
def draft_reply(conversation):
    if not conversation:
        frappe.throw(_("conversation is required"))
    ensure_can_read_conversation(conversation)
    return {"success": True, "result": generate_reply_draft(conversation)}
