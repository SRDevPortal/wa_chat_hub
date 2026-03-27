from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now_datetime

from wa_chat_hub.services import append_message, build_erp_actions, mark_conversation_read


@frappe.whitelist(methods=["POST"])
def ingest_message():
    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()

    required = ["channel_account"]
    missing = [field for field in required if not payload.get(field)]
    if missing:
        frappe.throw(_("Missing required fields: {0}").format(", ".join(missing)))

    result = append_message(payload)
    return {"success": True, "result": result}


@frappe.whitelist()
def get_conversations(limit=50, status=None, assigned_to=None, department=None):
    filters = {}
    if status:
        filters["status"] = status
    if assigned_to:
        filters["assigned_to"] = assigned_to
    if department:
        filters["department"] = department

    rows = frappe.get_all(
        "Chat Conversation",
        filters=filters,
        fields=[
            "name",
            "channel_account",
            "contact",
            "department",
            "assigned_to",
            "status",
            "priority",
            "last_message_preview",
            "unread_count",
            "modified",
        ],
        order_by="modified desc",
        limit_page_length=int(limit),
    )

    contact_names = [row.contact for row in rows if row.contact]
    contacts = {}
    if contact_names:
        for c in frappe.get_all("Chat Contact", filters={"name": ["in", contact_names]}, fields=["name", "display_name", "phone_number"]):
            contacts[c.name] = c

    return {
        "success": True,
        "result": [
            {
                **row,
                "contact_display_name": contacts.get(row.contact, {}).get("display_name"),
                "contact_phone_number": contacts.get(row.contact, {}).get("phone_number"),
            }
            for row in rows
        ],
    }


@frappe.whitelist()
def get_messages(conversation, limit=100):
    rows = frappe.get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=[
            "name",
            "direction",
            "sender_type",
            "content_type",
            "body",
            "media_url",
            "channel_message_id",
            "delivery_status",
            "creation",
        ],
        order_by="creation asc",
        limit_page_length=int(limit),
    )
    return {"success": True, "result": rows}


@frappe.whitelist(methods=["POST"])
def mark_read(conversation):
    mark_conversation_read(conversation)
    return {"success": True}


@frappe.whitelist()
def get_sidebar_context(conversation):
    convo = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", convo.contact)
    actions = build_erp_actions()
    return {
        "success": True,
        "result": {
            "conversation": convo.as_dict(),
            "contact": contact.as_dict(),
            "actions": actions,
            "server_time": str(now_datetime()),
        },
    }
