from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.desk.form import assign_to
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
    convo = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", convo.contact)
    actions = build_erp_actions()
    return {
        "success": True,
        "result": {
            "conversation": convo.as_dict(),
            "contact": contact.as_dict(),
            "attribution": get_conversation_attribution(conversation),
            "actions": actions,
            "server_time": str(now_datetime()),
        },
    }


def get_conversation_attribution(conversation):
    rows = frappe.get_all(
        "Chat Message",
        filters={"conversation": conversation, "direction": "Inbound"},
        fields=["raw_payload", "creation"],
        order_by="creation desc",
        limit_page_length=20,
    )
    for row in rows:
        payload = _json_loads(row.raw_payload)
        if not payload:
            continue
        data = _extract_attribution(payload)
        if any(data.values()):
            return data
    return {}


def _extract_attribution(payload):
    referral = _find_first_dict(payload, {"referral", "source", "context", "button", "click_to_whatsapp"})
    source_id = (
        _find_first_value(payload, ["source_id", "sourceId", "sourceID", "source_url_id", "Source ID"], referral)
        or _find_direct_value(referral, ["id"])
    )
    source = (
        _find_first_value(payload, ["_internal_lead_source", "internal_lead_source", "channel_type"], referral)
        or _find_first_value(payload, ["source", "source_type", "sourceType", "Source"], referral)
        or _find_direct_value(referral, ["type"])
    )
    return {
        "source_id": source_id,
        "source_url": _find_first_value(payload, ["source_url", "sourceUrl", "url", "sourceURL", "Source URL"], referral),
        "source": source,
        "ctwa_clid": _find_first_value(payload, ["ctwa_clid", "ctwaClid", "ctwa_click_id", "click_id", "ctwa clid"], referral),
    }


def _json_loads(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        return json.loads(value)
    except Exception:
        return {}


def _find_first_dict(value, preferred_keys):
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in preferred_keys and isinstance(nested, dict):
                return nested
        for nested in value.values():
            found = _find_first_dict(nested, preferred_keys)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_first_dict(nested, preferred_keys)
            if found:
                return found
    return {}


def _find_first_value(payload, keys, preferred=None):
    for source in (preferred or {}, payload):
        value = _find_value_recursive(source, set(keys))
        if value not in (None, ""):
            return str(value)
    return None


def _find_direct_value(value, keys):
    if not isinstance(value, dict):
        return None
    normalized = {_normalize_key(key) for key in keys}
    for key, found in value.items():
        if _normalize_key(key) in normalized and _is_scalar(found):
            return str(found)
    for key in keys:
        found = value.get(key)
        if _is_scalar(found):
            return str(found)
    return None


def _find_value_recursive(value, keys):
    normalized = {_normalize_key(key) for key in keys}
    if isinstance(value, dict):
        for key, nested in value.items():
            if _normalize_key(key) in normalized and _is_scalar(nested):
                return nested
        for nested in value.values():
            found = _find_value_recursive(nested, normalized)
            if found not in (None, ""):
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_value_recursive(nested, normalized)
            if found not in (None, ""):
                return found
    return None


def _is_scalar(value):
    return value not in (None, "") and not isinstance(value, (dict, list, tuple, set))


def _normalize_key(value):
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


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
