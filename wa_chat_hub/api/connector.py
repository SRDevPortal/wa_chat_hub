from __future__ import annotations

import frappe
from frappe import _

from wa_chat_hub.connector.registry import get_adapter
from wa_chat_hub.services import append_message


@frappe.whitelist(methods=["POST"])
def ingest_connector_event():
    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()

    channel_account = payload.get("channel_account")
    if not channel_account:
        frappe.throw(_("channel_account is required"))

    account = frappe.get_doc("Chat Channel Account", channel_account)
    adapter = get_adapter(account.channel_type)
    event_type = payload.get("event_type", "inbound_message")

    if event_type == "status_update":
        event = adapter.normalize_status(payload)
        return {"success": True, "result": event.__dict__}

    event = adapter.normalize_inbound(payload)
    result = append_message(event.__dict__)
    return {"success": True, "result": result}
