from __future__ import annotations

import frappe
from frappe import _

from wa_chat_hub.mcp import build_mcp_runtime_context, invoke_mcp_tool
from wa_chat_hub.outbound import build_outbound_message_payload
from wa_chat_hub.services import append_message


@frappe.whitelist()
def get_runtime_context(department=None):
    return {"success": True, "result": build_mcp_runtime_context(department=department)}


@frappe.whitelist(methods=["POST"])
def send_reply():
    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()

    conversation = payload.get("conversation")
    body = payload.get("body")
    if not conversation:
        frappe.throw(_("conversation is required"))
    if not body:
        frappe.throw(_("body is required"))

    outbound = build_outbound_message_payload(conversation, body, payload.get("content_type", "Text"))

    # Store outgoing message immediately so desk remains operational even before transport worker wiring.
    convo = frappe.get_doc("Chat Conversation", conversation)
    append_message({
        "channel_account": convo.channel_account,
        "phone_number": frappe.db.get_value("Chat Contact", convo.contact, "phone_number"),
        "direction": "Outbound",
        "sender_type": payload.get("sender_type", "Agent"),
        "content_type": payload.get("content_type", "Text"),
        "body": body,
        "delivery_status": "Pending",
        "raw_transport_payload": outbound,
    })
    return {"success": True, "result": outbound}


@frappe.whitelist(methods=["POST"])
def call_mcp_tool():
    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()
    return invoke_mcp_tool(
        server_name=payload.get("server_name"),
        tool_name=payload.get("tool_name"),
        payload=payload.get("payload") or {},
    )
