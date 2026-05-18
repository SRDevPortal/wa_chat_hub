from __future__ import annotations

from typing import Any, Dict, Optional

import frappe


DEFAULT_CONVERSATION_STATUS = "Open"


def normalize_phone(phone: Optional[str]) -> str:
    if not phone:
        return ""
    return "".join(ch for ch in str(phone) if ch.isdigit())


def classify_department(channel_department: Optional[str], detected_department: Optional[str] = None) -> Optional[str]:
    return detected_department or channel_department


def route_conversation(payload: Dict[str, Any]) -> Dict[str, Any]:
    department = classify_department(
        payload.get("channel_department"),
        payload.get("detected_department"),
    )
    assigned_to = payload.get("assigned_to") or find_assignment_owner(
        department=department,
        channel_account=payload.get("channel_account"),
        priority=payload.get("priority"),
    )
    return {
        "department": department,
        "assigned_to": assigned_to,
        "queue_status": payload.get("queue_status", DEFAULT_CONVERSATION_STATUS),
    }


def find_assignment_owner(
    department: Optional[str] = None,
    channel_account: Optional[str] = None,
    priority: Optional[str] = None,
) -> Optional[str]:
    filters = {"is_active": 1}
    if department:
        filters["department"] = department
    if channel_account:
        filters["channel_account"] = channel_account
    if priority:
        filters["priority"] = priority

    rows = frappe.get_all(
        "Chat Assignment Rule",
        filters=filters,
        fields=["assign_to"],
        limit=1,
    )
    return rows[0].assign_to if rows else None


def get_or_create_contact(phone_number: str, display_name: Optional[str] = None) -> str:
    normalized = normalize_phone(phone_number)
    existing = frappe.db.get_value("Chat Contact", {"phone_number": normalized}, "name")
    if existing:
        if display_name:
            frappe.db.set_value("Chat Contact", existing, "display_name", display_name)
        return existing

    doc = frappe.get_doc({
        "doctype": "Chat Contact",
        "phone_number": normalized,
        "display_name": display_name or normalized,
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def get_or_create_conversation(
    channel_account: str,
    contact: str,
    department: Optional[str] = None,
    assigned_to: Optional[str] = None,
    status: str = DEFAULT_CONVERSATION_STATUS,
) -> str:
    existing = frappe.db.get_value(
        "Chat Conversation",
        {"channel_account": channel_account, "contact": contact, "status": ["!=", "Closed"]},
        "name",
    )
    if existing:
        updates = {}
        if department:
            updates["department"] = department
        if assigned_to:
            updates["assigned_to"] = assigned_to
        if updates:
            frappe.db.set_value("Chat Conversation", existing, updates)
        return existing

    doc = frappe.get_doc({
        "doctype": "Chat Conversation",
        "channel_account": channel_account,
        "contact": contact,
        "department": department,
        "assigned_to": assigned_to,
        "status": status,
    })
    doc.insert(ignore_permissions=True)
    return doc.name


def append_message(payload: Dict[str, Any]) -> Dict[str, str]:
    phone_number = normalize_phone(payload.get("phone_number") or payload.get("to") or payload.get("from"))
    contact = get_or_create_contact(phone_number=phone_number, display_name=payload.get("display_name"))

    channel_account = payload["channel_account"]
    routing = route_conversation({
        "channel_department": payload.get("channel_department"),
        "detected_department": payload.get("detected_department"),
        "channel_account": channel_account,
        "priority": payload.get("priority"),
    })

    conversation = get_or_create_conversation(
        channel_account=channel_account,
        contact=contact,
        department=routing.get("department"),
        assigned_to=routing.get("assigned_to"),
        status=routing.get("queue_status") or DEFAULT_CONVERSATION_STATUS,
    )

    direction = payload.get("direction", "Inbound")
    delivery_status = payload.get("delivery_status")
    if not delivery_status:
        delivery_status = "Received" if direction == "Inbound" else "Pending"

    message = frappe.get_doc({
        "doctype": "Chat Message",
        "conversation": conversation,
        "direction": direction,
        "sender_type": payload.get("sender_type", "Customer"),
        "content_type": payload.get("content_type", "Text"),
        "body": payload.get("body"),
        "media_url": payload.get("media_url"),
        "channel_message_id": payload.get("channel_message_id"),
        "delivery_status": delivery_status,
        "raw_payload": frappe.as_json(payload),
        "raw_transport_payload": frappe.as_json(payload.get("raw_transport_payload") or {}),
    })
    message.insert(ignore_permissions=True)

    update_conversation_after_message(conversation, payload)
    frappe.publish_realtime(
        "wa_chat_new_message",
        {
            "conversation": conversation,
            "message": message.as_dict(),
            "direction": message.direction,
        },
        after_commit=True,
    )
    return {"contact": contact, "conversation": conversation, "message": message.name}


def cint_safe(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def update_conversation_after_message(conversation_name: str, payload: Dict[str, Any]) -> None:
    convo = frappe.get_doc("Chat Conversation", conversation_name)
    body = payload.get("body")
    content_type = payload.get("content_type") or "Text"
    media_url = payload.get("media_url")
    if media_url and content_type != "Text":
        preview = build_media_preview(content_type, body)
    else:
        preview = body or content_type or ""
    convo.last_message_preview = preview[:500]
    unread = cint_safe(convo.unread_count)
    if payload.get("direction", "Inbound") == "Inbound":
        convo.unread_count = unread + 1
    convo.save(ignore_permissions=True)


def build_media_preview(content_type: str, body: Optional[str] = None) -> str:
    normalized_type = str(content_type or "Media").title()
    clean_body = _clean_media_body(normalized_type, body)
    if normalized_type == "Image":
        return f"[Image] {clean_body or 'Photo'}"
    if normalized_type == "Document":
        return f"[Document] {clean_body or 'Document'}"
    return f"[{normalized_type}] {clean_body or normalized_type}"


def _clean_media_body(content_type: str, body: Optional[str]) -> str:
    text = str(body or "").strip()
    normalized = text.lower()
    generic_by_type = {
        "Image": {"", "none", "null", "undefined", "photo", "image", "image message received", "[image message received]"},
        "Document": {"", "none", "null", "undefined", "document", "document message received", "[document message received]"},
    }
    if normalized in generic_by_type.get(content_type, {"", "none", "null", "undefined"}):
        return ""
    return text


def mark_conversation_read(conversation_name: str) -> None:
    frappe.db.set_value("Chat Conversation", conversation_name, "unread_count", 0)
    frappe.publish_realtime(
        "wa_chat_conversation_updated",
        {"conversation": conversation_name, "unread_count": 0},
        after_commit=True,
    )


def repair_inbound_pending_statuses() -> None:
    frappe.db.sql("""
        update `tabChat Message`
        set delivery_status = 'Received'
        where direction = 'Inbound'
          and delivery_status = 'Pending'
    """)
    frappe.db.commit()


def build_erp_actions() -> Dict[str, Dict[str, str]]:
    return {
        "lead": {"label": "Create Lead", "doctype": "Lead"},
        "encounter": {"label": "Create Encounter", "doctype": "Patient Encounter"},
        "support_ticket": {"label": "Create Support Ticket", "doctype": "Issue"},
        "patient": {"label": "Link/Create Patient", "doctype": "Patient"},
    }
