from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
import requests
from frappe.utils.file_manager import save_file

from wa_chat_hub.ai.lead_scoring import score_and_sync_conversation
from wa_chat_hub.ai.ocr_summary import build_attachment_filename, process_attachment_for_lead_summary
from wa_chat_hub.prompts import (
    get_conversation_crm_lead,
    get_conversation_linked_reference,
    set_conversation_crm_lead,
)


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
    if direction == "Inbound":
        _link_or_create_master_record(
            conversation=conversation,
            contact_name=contact,
            phone_number=phone_number,
            display_name=payload.get("display_name"),
        )

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
    attachment_file = None
    try:
        attachment_file = _persist_inbound_attachment(message, payload)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Inbound Attachment Persistence Failed")

    update_conversation_after_message(conversation, payload)
    try:
        from wa_chat_hub.messaging.windows import update_windows_on_message

        update_windows_on_message(
            conversation,
            direction=direction,
            sender_type=payload.get("sender_type", "Customer"),
            content_type=payload.get("content_type", "Text"),
            raw_payload=payload.get("raw_payload") or payload,
            message_time=str(message.creation),
            template_category=payload.get("template_category"),
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Messaging Window Update Failed")
    try:
        score_and_sync_conversation(conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Lead Scoring Update Failed")
    if attachment_file and direction == "Inbound":
        try:
            _sync_inbound_attachment_to_linked_record(
                conversation=conversation,
                chat_file_name=attachment_file,
                message_name=message.name,
                payload=payload,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "CRM Lead Attachment Sync Failed")
        try:
            process_attachment_for_lead_summary(conversation, message.name, payload)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "OCR Lead Summary Failed")
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


def _link_or_create_master_record(
    conversation: str,
    contact_name: str,
    phone_number: str,
    display_name: Optional[str] = None,
) -> None:
    """Attach inbound chat to existing Patient/Customer else create a Lead."""
    if not phone_number:
        return

    convo = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", contact_name)
    _sanitize_contact_links(contact)
    _sanitize_conversation_links(convo)
    _normalize_existing_lead_link(convo)
    if get_conversation_crm_lead(convo):
        return
    ref_dt, ref_name = get_conversation_linked_reference(convo)
    if ref_dt and ref_name and ref_dt not in {"CRM Lead", "Lead"}:
        return

    patient_name = _find_by_phone("Patient", ["mobile", "mobile_no", "phone", "custom_whatsapp_number"], phone_number)
    if patient_name:
        contact.linked_patient = patient_name
        contact.source_doctype = "Patient"
        contact.source_name = patient_name
        if display_name and not contact.display_name:
            contact.display_name = display_name
        contact.save(ignore_permissions=True)
        convo.linked_reference_doctype = "Patient"
        convo.linked_reference_name = patient_name
        convo.save(ignore_permissions=True)
        return

    customer_name = _find_by_phone("Customer", ["mobile_no", "phone", "custom_whatsapp_number"], phone_number)
    if customer_name:
        contact.source_doctype = "Customer"
        contact.source_name = customer_name
        if display_name and not contact.display_name:
            contact.display_name = display_name
        contact.save(ignore_permissions=True)
        convo.linked_reference_doctype = "Customer"
        convo.linked_reference_name = customer_name
        convo.save(ignore_permissions=True)
        return

    lead_doctype = _preferred_lead_doctype()
    if not lead_doctype:
        return

    existing_lead = _find_existing_lead_by_phone(phone_number)
    lead_name = existing_lead or _create_lead_for_inbound(
        doctype=lead_doctype,
        phone_number=phone_number,
        display_name=display_name or contact.display_name,
        channel_account=convo.channel_account,
    )

    contact.linked_lead = lead_name if lead_doctype == "Lead" else None
    contact.source_doctype = lead_doctype
    contact.source_name = lead_name
    if display_name and not contact.display_name:
        contact.display_name = display_name
    contact.save(ignore_permissions=True)

    if lead_doctype == "CRM Lead":
        set_conversation_crm_lead(convo, lead_name)
    else:
        convo.linked_reference_doctype = lead_doctype
        convo.linked_reference_name = lead_name
    convo.save(ignore_permissions=True)


def _create_lead_for_inbound(
    doctype: str,
    phone_number: str,
    display_name: Optional[str],
    channel_account: Optional[str] = None,
) -> str:
    payload: Dict[str, Any] = {"doctype": doctype}
    meta = frappe.get_meta(doctype)
    lead_title = display_name or phone_number
    if meta.has_field("lead_name"):
        payload["lead_name"] = lead_title
    if meta.has_field("first_name") and not payload.get("lead_name"):
        payload["first_name"] = lead_title
    if meta.has_field("mobile_no"):
        payload["mobile_no"] = phone_number
    elif meta.has_field("phone"):
        payload["phone"] = phone_number
    if meta.has_field("source"):
        source_value = _resolve_whatsapp_source_value(meta)
        if source_value:
            payload["source"] = source_value

    pipeline = _get_mapped_sr_pipeline(channel_account)
    pipeline_fieldname = _get_lead_pipeline_fieldname(doctype)
    if pipeline and pipeline_fieldname:
        payload[pipeline_fieldname] = pipeline
    elif pipeline and not pipeline_fieldname:
        frappe.log_error(
            f"Mapped SR Lead Pipeline '{pipeline}' for channel '{channel_account}', but no Link field to SR Lead Pipeline found on {doctype}.",
            "WA Channel Pipeline Mapping",
        )

    doc = frappe.get_doc(payload)
    doc.insert(ignore_permissions=True)
    return doc.name


def _find_by_phone(doctype: str, phone_fields: list[str], phone_number: str) -> Optional[str]:
    if not frappe.db.exists("DocType", doctype):
        return None

    meta = frappe.get_meta(doctype)
    for fieldname in phone_fields:
        if not meta.has_field(fieldname):
            continue
        exact = frappe.db.get_value(doctype, {fieldname: phone_number}, "name")
        if exact:
            return exact

        last10 = phone_number[-10:] if len(phone_number) >= 10 else phone_number
        candidates = frappe.get_all(
            doctype,
            filters={fieldname: ["like", f"%{last10}%"]},
            fields=["name", fieldname],
            limit_page_length=20,
        )
        for row in candidates:
            value = normalize_phone(row.get(fieldname))
            if not value:
                continue
            if value == phone_number or value.endswith(last10):
                return row.name
    return None


def _get_mapped_sr_pipeline(channel_account: Optional[str]) -> Optional[str]:
    if not channel_account:
        return None
    return frappe.db.get_value(
        "WA Channel Pipeline Map",
        {"chat_channel_account": channel_account, "is_active": 1},
        "sr_lead_pipeline",
    )


def _get_lead_pipeline_fieldname(lead_doctype: str) -> Optional[str]:
    """Auto-detect first Link field on Lead/CRM Lead targeting SR Lead Pipeline."""
    if not frappe.db.exists("DocType", "SR Lead Pipeline"):
        return None
    meta = frappe.get_meta(lead_doctype)
    for field in meta.fields:
        if field.fieldtype == "Link" and field.options == "SR Lead Pipeline":
            return field.fieldname
    return None


def _preferred_lead_doctype() -> Optional[str]:
    if frappe.db.exists("DocType", "CRM Lead"):
        return "CRM Lead"
    if frappe.db.exists("DocType", "Lead"):
        return "Lead"
    return None


def _find_existing_lead_by_phone(phone_number: str) -> Optional[str]:
    for doctype in ("CRM Lead", "Lead"):
        if not frappe.db.exists("DocType", doctype):
            continue
        found = _find_by_phone(doctype, ["mobile_no", "phone", "custom_whatsapp_number"], phone_number)
        if found:
            return found
    return None


def _normalize_existing_lead_link(convo) -> None:
    """If record points to Lead but name exists in CRM Lead, relink to CRM Lead route."""
    if not frappe.db.exists("DocType", "CRM Lead"):
        return
    lead_name = get_conversation_crm_lead(convo)
    if lead_name:
        set_conversation_crm_lead(convo, lead_name)
        convo.save(ignore_permissions=True)
        return
    if convo.linked_reference_doctype != "Lead" or not convo.linked_reference_name:
        return
    if frappe.db.exists("CRM Lead", convo.linked_reference_name):
        set_conversation_crm_lead(convo, convo.linked_reference_name)
        convo.save(ignore_permissions=True)


def _sanitize_contact_links(contact) -> None:
    changed = False

    linked_lead = getattr(contact, "linked_lead", None)
    if linked_lead:
        lead_exists = frappe.db.exists("DocType", "Lead") and frappe.db.exists("Lead", linked_lead)
        if not lead_exists:
            contact.linked_lead = None
            changed = True

    linked_patient = getattr(contact, "linked_patient", None)
    if linked_patient and frappe.db.exists("DocType", "Patient") and not frappe.db.exists("Patient", linked_patient):
        contact.linked_patient = None
        changed = True

    if changed:
        contact.save(ignore_permissions=True)


def _sanitize_conversation_links(convo) -> None:
    crm_lead = getattr(convo, "linked_crm_lead", None)
    if crm_lead and not frappe.db.exists("CRM Lead", crm_lead):
        convo.linked_crm_lead = None
        convo.linked_reference_doctype = None
        convo.linked_reference_name = None
        convo.save(ignore_permissions=True)
        return

    ref_doctype = getattr(convo, "linked_reference_doctype", None)
    ref_name = getattr(convo, "linked_reference_name", None)
    if not ref_doctype or not ref_name:
        return
    if not frappe.db.exists("DocType", ref_doctype):
        convo.linked_reference_doctype = None
        convo.linked_reference_name = None
        convo.save(ignore_permissions=True)
        return
    if not frappe.db.exists(ref_doctype, ref_name):
        convo.linked_reference_doctype = None
        convo.linked_reference_name = None
        convo.save(ignore_permissions=True)


def _resolve_whatsapp_source_value(meta) -> Optional[str]:
    """Return safe source value for target lead doctype, or None if unavailable."""
    source_df = next((f for f in meta.fields if f.fieldname == "source"), None)
    if not source_df:
        return None

    # For Link fields, ensure the linked master row exists.
    if source_df.fieldtype == "Link" and source_df.options:
        if frappe.db.exists(source_df.options, "WhatsApp"):
            return "WhatsApp"
        # If WhatsApp option is absent, don't set source and avoid LinkValidationError.
        return None

    # For Select fields, set only if WhatsApp exists in options.
    if source_df.fieldtype == "Select":
        options = [opt.strip() for opt in str(source_df.options or "").split("\n") if opt.strip()]
        if "WhatsApp" in options:
            return "WhatsApp"
        return None

    # Data/other field types can safely take literal value.
    return "WhatsApp"


def _persist_inbound_attachment(message_doc, payload: Dict[str, Any]) -> Optional[str]:
    """Persist inbound media as File linked to Chat Message."""
    if str(payload.get("direction") or "").title() != "Inbound":
        return None
    media_url = str(payload.get("media_url") or "").strip()
    if not media_url:
        return None

    filename = build_attachment_filename(payload, media_url)
    existing = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": "Chat Message",
            "attached_to_name": message_doc.name,
            "file_url": media_url,
        },
        "name",
    )
    if existing:
        if frappe.get_meta("Chat Message").has_field("attachment_file"):
            frappe.db.set_value("Chat Message", message_doc.name, "attachment_file", existing, update_modified=False)
        return existing

    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": filename,
            "file_url": media_url,
            "is_private": 0,
            "attached_to_doctype": "Chat Message",
            "attached_to_name": message_doc.name,
        }
    )
    file_doc.insert(ignore_permissions=True)
    if frappe.get_meta("Chat Message").has_field("attachment_file"):
        frappe.db.set_value("Chat Message", message_doc.name, "attachment_file", file_doc.name, update_modified=False)
    return file_doc.name


def _sync_inbound_attachment_to_linked_record(
    conversation: str,
    chat_file_name: str,
    message_name: str,
    payload: Dict[str, Any],
) -> None:
    """Mirror inbound chat media on linked CRM Lead (form-attachments sidebar)."""
    convo = frappe.get_doc("Chat Conversation", conversation)
    crm_lead = get_conversation_crm_lead(convo)
    if not crm_lead:
        return
    ref_doctype = "CRM Lead"
    ref_name = crm_lead

    media_url = str(payload.get("media_url") or "").strip()
    if not media_url:
        return

    chat_file = frappe.get_doc("File", chat_file_name)
    filename = build_attachment_filename(payload, media_url)
    lead_filename = f"WA-{message_name}-{filename}"
    if frappe.db.exists(
        "File",
        {
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
            "file_name": lead_filename,
        },
    ):
        return
    if frappe.db.exists(
        "File",
        {
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
            "file_url": chat_file.file_url,
        },
    ):
        return
    content = _fetch_media_bytes(media_url)
    if content:
        save_file(lead_filename, content, ref_doctype, ref_name, is_private=0)
        return

    chat_file.create_attachment_copy(ref_doctype, ref_name, ignore_permissions=True)


def _fetch_media_bytes(media_url: str) -> Optional[bytes]:
    try:
        response = requests.get(media_url, timeout=30)
        if response.ok and response.content:
            return response.content
    except Exception:
        pass
    return None
