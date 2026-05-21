from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.desk.form import assign_to
from frappe.utils import now_datetime

from wa_chat_hub.messaging.attribution import get_conversation_attribution
from wa_chat_hub.messaging.windows import get_messaging_window_state
from wa_chat_hub.services import append_message, build_erp_actions, mark_conversation_read
from wa_chat_hub.services import normalize_phone


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
def get_channel_accounts():
    rows = frappe.get_all(
        "Chat Channel Account",
        filters={"is_active": 1},
        fields=["name", "account_name", "channel_type", "phone_number", "connector_status"],
        order_by="account_name asc",
    )
    return {"success": True, "result": rows}


@frappe.whitelist()
def get_conversations(limit=50, status=None, assigned_to=None, department=None, channel_account=None):
    filters = {}
    if status:
        filters["status"] = status
    if assigned_to:
        filters["assigned_to"] = assigned_to
    if department:
        filters["department"] = department
    if channel_account and str(channel_account).strip().lower() not in {"", "all", "__all__"}:
        filters["channel_account"] = channel_account

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
            "lead_score",
            "lead_lan",
            "lead_temperature",
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
            "attachment_file",
            "channel_message_id",
            "delivery_status",
            "raw_transport_payload",
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


@frappe.whitelist(methods=["POST"])
def bulk_assign(conversations, user=None):
    names = _as_list(conversations)
    if not names:
        frappe.throw(_("Select at least one conversation"))

    for name in names:
        _ensure_conversation_write(name)
        frappe.db.set_value("Chat Conversation", name, "assigned_to", user or None)
        assign_to.clear("Chat Conversation", name)
        if user:
            assign_to.add(
                {
                    "assign_to": [user],
                    "doctype": "Chat Conversation",
                    "name": name,
                    "description": _("WhatsApp conversation assigned"),
                    "notify": 0,
                },
                ignore_permissions=True,
            )

    frappe.db.commit()
    return {"success": True, "updated": len(names)}


@frappe.whitelist(methods=["POST"])
def bulk_update(conversations, fieldname, value):
    if fieldname not in {"status", "priority"}:
        frappe.throw(_("Invalid field for bulk update"))

    valid_values = {
        "status": {"Open", "Pending", "Resolved", "Closed"},
        "priority": {"Low", "Medium", "High", "Urgent"},
    }
    if value not in valid_values[fieldname]:
        frappe.throw(_("Invalid value for {0}").format(fieldname))

    names = _as_list(conversations)
    if not names:
        frappe.throw(_("Select at least one conversation"))

    for name in names:
        _ensure_conversation_write(name)
        frappe.db.set_value("Chat Conversation", name, fieldname, value)

    frappe.db.commit()
    return {"success": True, "updated": len(names)}


@frappe.whitelist(methods=["POST"])
def add_external_outbound_message(conversation, body, delivery_status="Sent", channel_message_id=None):
    if not conversation:
        frappe.throw(_("conversation is required"))
    if not body:
        frappe.throw(_("body is required"))
    _ensure_conversation_write(conversation)

    convo = frappe.get_doc("Chat Conversation", conversation)
    if channel_message_id and frappe.db.exists("Chat Message", {"channel_message_id": channel_message_id}):
        return {"success": True, "message": "Duplicate message ignored"}

    result = append_message({
        "channel_account": convo.channel_account,
        "phone_number": frappe.db.get_value("Chat Contact", convo.contact, "phone_number"),
        "direction": "Outbound",
        "sender_type": "Agent",
        "content_type": "Text",
        "body": body,
        "delivery_status": delivery_status or "Sent",
        "channel_message_id": channel_message_id,
        "raw_payload": {"source": "manual_interakt_sync"},
    })
    return {"success": True, "result": result}


@frappe.whitelist()
def get_sidebar_context(conversation):
    from wa_chat_hub.messaging.windows import _ensure_messaging_window_schema

    _ensure_messaging_window_schema()
    convo = frappe.get_doc("Chat Conversation", conversation)
    try:
        convo.reload()
    except Exception:
        pass
    contact = frappe.get_doc("Chat Contact", convo.contact)
    actions = build_erp_actions()
    messaging_window = get_messaging_window_state(conversation, convo=convo)
    attribution = get_conversation_attribution(conversation)
    persisted_attribution = tuple(
        getattr(convo, key, None) for key in ("source_id", "source_url", "source", "ctwa_clid")
    )
    if not any(persisted_attribution):
        for key in ("source_id", "source_url", "source", "ctwa_clid"):
            if messaging_window.get(key):
                attribution[key] = messaging_window[key]

    return {
        "success": True,
        "result": {
            "conversation": convo.as_dict(),
            "contact": contact.as_dict(),
            "attribution": attribution,
            "messaging_window": messaging_window,
            "actions": actions,
            "server_time": str(now_datetime()),
        },
    }


@frappe.whitelist()
def get_messaging_window(conversation):
    if not conversation:
        frappe.throw(_("conversation is required"))
    return {"success": True, "result": get_messaging_window_state(conversation)}


@frappe.whitelist()
def resolve_chat_for_reference(reference_doctype, reference_name=None, phone_number=None):
    if not reference_doctype:
        frappe.throw(_("reference_doctype is required"))

    if reference_name:
        conv = None
        if reference_doctype == "CRM Lead" and frappe.get_meta("Chat Conversation").has_field(
            "linked_crm_lead"
        ):
            conv = frappe.db.get_value(
                "Chat Conversation",
                {"linked_crm_lead": reference_name},
                "name",
            )
        if not conv:
            conv = frappe.db.get_value(
                "Chat Conversation",
                {
                    "linked_reference_doctype": reference_doctype,
                    "linked_reference_name": reference_name,
                },
                "name",
            )
        if conv:
            return {"success": True, "result": {"conversation": conv}}

    normalized = normalize_phone(phone_number)
    if normalized:
        contact = frappe.db.get_value("Chat Contact", {"phone_number": normalized}, "name")
        if contact:
            conv = frappe.db.get_value("Chat Conversation", {"contact": contact, "status": ["!=", "Closed"]}, "name")
            if conv:
                return {"success": True, "result": {"conversation": conv}}

    return {"success": True, "result": {"conversation": None}}


def _as_list(value):
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except Exception:
            value = [value]
    if not isinstance(value, list):
        return []
    return [item.get("name") if isinstance(item, dict) else item for item in value if item]


def _ensure_conversation_write(name):
    if not frappe.has_permission("Chat Conversation", "write", name):
        frappe.throw(_("Not permitted to update conversation {0}").format(name), frappe.PermissionError)
