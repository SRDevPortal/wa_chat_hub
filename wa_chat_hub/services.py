from __future__ import annotations

import json
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import frappe
from frappe import _

from wa_chat_hub.ai.ocr_summary import build_attachment_filename, process_attachment_for_lead_summary
from wa_chat_hub.ai.media_transcription import (
    TRANSCRIPT_CONTENT_TYPES,
    process_transcript_for_lead_summary,
)
from wa_chat_hub.db_retry import is_db_lock_conflict, with_db_lock_retry
from wa_chat_hub.prompts import (
    get_conversation_crm_lead,
    get_conversation_linked_reference,
    set_conversation_crm_lead,
)
from wa_chat_hub.task_logger import elapsed, task_log


DEFAULT_CONVERSATION_STATUS = "Open"
WA_LEAD_CONTEXT_MARKER = "WA_CHAT_HUB_CONTEXT_JSON"
WA_LEAD_PAYLOAD_MARKER = "WA_CHAT_HUB_PAYLOAD_JSON"


@contextmanager
def _crm_lead_field_guard_bypass(enabled: bool = True):
    """Temporarily allow trusted WA automation through CRM Lead field guards."""
    previous = getattr(frappe.flags, "sr_bypass_field_guard", False)
    if enabled:
        frappe.flags.sr_bypass_field_guard = True
    try:
        yield
    finally:
        frappe.flags.sr_bypass_field_guard = previous


def _json_block(marker: str, value: Dict[str, Any]) -> str:
    return (
        f"\n\n--- {marker} ---\n"
        f"{json.dumps(value, indent=2, sort_keys=True, default=str, ensure_ascii=False)}\n"
        f"--- END_{marker} ---"
    )


def _lead_creation_error_details(traceback: str, payload: Dict[str, Any], context: Dict[str, Any]) -> str:
    return (
        traceback
        + _json_block(WA_LEAD_CONTEXT_MARKER, context)
        + _json_block(WA_LEAD_PAYLOAD_MARKER, payload)
    )


def normalize_phone(phone: Optional[str]) -> str:
    if not phone:
        return ""
    return "".join(ch for ch in str(phone) if ch.isdigit())


def _valid_link(doctype: str, value: Optional[str]) -> Optional[str]:
    """Return value only if it exists in the linked DocType (avoids webhook hard-fail)."""
    name = (value or "").strip()
    if not name:
        return None
    if frappe.db.exists(doctype, name):
        return name
    frappe.logger("wa_chat_hub").warning(
        "Ignored invalid %s link on Chat Conversation: %s", doctype, name
    )
    return None


def classify_department(channel_department: Optional[str], detected_department: Optional[str] = None) -> Optional[str]:
    return _valid_link("Department", detected_department or channel_department)


def route_conversation(payload: Dict[str, Any]) -> Dict[str, Any]:
    department = classify_department(
        payload.get("channel_department"),
        payload.get("detected_department"),
    )
    assigned_to = _valid_link(
        "User",
        payload.get("assigned_to")
        or find_assignment_owner(
            department=department,
            channel_account=payload.get("channel_account"),
            priority=payload.get("priority"),
        ),
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
    try:
        doc.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        existing = frappe.db.get_value("Chat Contact", {"phone_number": normalized}, "name") or normalized
        if display_name and frappe.db.exists("Chat Contact", existing):
            frappe.db.set_value("Chat Contact", existing, "display_name", display_name)
        return existing
    return doc.name


def get_or_create_conversation(
    channel_account: str,
    contact: str,
    department: Optional[str] = None,
    assigned_to: Optional[str] = None,
    status: str = DEFAULT_CONVERSATION_STATUS,
) -> str:
    filters = {"channel_account": channel_account, "contact": contact, "status": ["!=", "Closed"]}

    existing = with_db_lock_retry(
        "conversation_lookup",
        lambda: frappe.db.get_value("Chat Conversation", filters, "name"),
    )
    if existing:
        updates = {}
        if department:
            updates["department"] = department
        if assigned_to:
            updates["assigned_to"] = assigned_to
        if updates:
            with_db_lock_retry(
                "conversation_routing_update",
                lambda: frappe.db.set_value("Chat Conversation", existing, updates),
            )
        return existing

    def _insert_conversation() -> str:
        doc = frappe.get_doc({
            "doctype": "Chat Conversation",
            "channel_account": channel_account,
            "contact": contact,
            "department": department,
            "assigned_to": assigned_to,
            "status": status,
        })
        try:
            doc.insert(ignore_permissions=True)
            return doc.name
        except Exception as exc:
            if not is_db_lock_conflict(exc):
                raise
            concurrent = frappe.db.get_value("Chat Conversation", filters, "name")
            if concurrent:
                return concurrent
            raise

    return with_db_lock_retry("conversation_insert", _insert_conversation)


def append_message(payload: Dict[str, Any]) -> Dict[str, str]:
    started = time.monotonic()
    task_log(
        "message",
        "append_start",
        direction=payload.get("direction", "Inbound"),
        sender_type=payload.get("sender_type", "Customer"),
        content_type=payload.get("content_type", "Text"),
        channel_account=payload.get("channel_account"),
    )
    frappe.flags.wa_chat_in_append_message = True
    frappe.local.wa_chat_in_append_message = True
    try:
        result = _append_message_impl(payload)
        task_log(
            "message",
            "append_done",
            direction=payload.get("direction", "Inbound"),
            conversation=result.get("conversation"),
            message=result.get("message"),
            duration_sec=elapsed(started),
        )
        return result
    except Exception as exc:
        task_log(
            "message",
            "append_failed",
            direction=payload.get("direction", "Inbound"),
            duration_sec=elapsed(started),
            error=str(exc)[:140],
        )
        raise
    finally:
        frappe.flags.wa_chat_in_append_message = False
        frappe.local.wa_chat_in_append_message = False


def _append_message_impl(payload: Dict[str, Any]) -> Dict[str, str]:
    phone_number = normalize_phone(payload.get("phone_number") or payload.get("to") or payload.get("from"))
    if not phone_number:
        frappe.throw(_("Cannot store WhatsApp message: customer phone number is missing in webhook payload."))
    contact = get_or_create_contact(phone_number=phone_number, display_name=payload.get("display_name"))

    channel_account = payload["channel_account"]
    existing_conversation = frappe.db.get_value(
        "Chat Conversation",
        {"channel_account": channel_account, "contact": contact, "status": ["!=", "Closed"]},
        "name",
    )

    # Preserve existing conversations: map defaults apply only when creating a new thread.
    # Chat Conversation.department → ERPNext "Department", not Medical Department.
    # sr_medical_department on WA Channel Pipeline Map is only for Patient routing / Interakt traits.
    channel_department = _valid_link("Department", payload.get("channel_department"))
    if not channel_department and not existing_conversation:
        account_department = frappe.db.get_value(
            "Chat Channel Account", channel_account, "department"
        )
        channel_department = _valid_link("Department", account_department)

    routing = route_conversation({
        "channel_department": channel_department,
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

    # Open 24h window before insert so AI autopilot (after_insert hook) sees an active window.
    if direction == "Inbound":
        try:
            from frappe.utils import now_datetime
            from wa_chat_hub.messaging.windows import update_windows_on_message

            update_windows_on_message(
                conversation,
                direction=direction,
                sender_type=payload.get("sender_type", "Customer"),
                content_type=payload.get("content_type", "Text"),
                raw_payload=payload.get("raw_payload") or payload,
                message_time=str(now_datetime()),
                template_category=payload.get("template_category"),
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Messaging Window Update Failed")

    message.insert(ignore_permissions=True)

    if direction == "Inbound":
        try:
            _link_or_create_master_record(
                conversation=conversation,
                contact_name=contact,
                phone_number=phone_number,
                display_name=payload.get("display_name"),
                raw_payload=_coerce_inbound_raw_payload(payload),
                message_name=message.name,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "WA Chat Hub Inbound Link Failed",
            )

    attachment_file = None
    try:
        attachment_file = _persist_inbound_attachment(message, payload)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Inbound Attachment Persistence Failed")

    update_conversation_after_message(conversation, payload)
    if direction == "Inbound":
        try:
            _enqueue_lead_scoring(conversation)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Lead Scoring Enqueue Failed")
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
        content_type = str(payload.get("content_type") or "").title()
        if content_type in TRANSCRIPT_CONTENT_TYPES:
            try:
                process_transcript_for_lead_summary(conversation, message.name, payload)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "Media Transcript Lead Summary Failed")
        else:
            try:
                process_attachment_for_lead_summary(conversation, message.name, payload)
            except Exception:
                frappe.log_error(frappe.get_traceback(), "OCR Lead Summary Failed")
    if direction == "Inbound" and not (
        getattr(frappe.flags, "wa_ai_outbound_reply", False)
        or getattr(frappe.local, "wa_ai_outbound_reply", False)
    ):
        try:
            from wa_chat_hub.api.ai_bot import schedule_autopilot_for_message

            schedule_autopilot_for_message(message.name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Autopilot Schedule Failed")
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


def _enqueue_lead_scoring(conversation: str) -> None:
    """Score after commit so inbound webhooks are not held by lead/LLM work."""
    if not conversation:
        return

    frappe.enqueue(
        "wa_chat_hub.ai.lead_scoring.score_and_sync_conversation",
        queue="short",
        conversation=conversation,
        timeout=90,
        enqueue_after_commit=True,
        now=frappe.flags.in_test,
        job_id=f"wa_lead_score_{conversation}",
        deduplicate=True,
    )
    task_log("lead_score", "enqueue", conversation=conversation, queue="short")


def cint_safe(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def update_conversation_after_message(conversation_name: str, payload: Dict[str, Any]) -> None:
    """Update preview/unread without full doc save (avoids TimestampMismatch under concurrent updates)."""
    body = payload.get("body")
    content_type = payload.get("content_type") or "Text"
    media_url = payload.get("media_url")
    if media_url and content_type != "Text":
        preview = build_media_preview(content_type, body)
    else:
        preview = body or content_type or ""

    if payload.get("direction", "Inbound") == "Inbound":
        with_db_lock_retry(
            "conversation_unread_increment",
            lambda: frappe.db.sql(
                """
                UPDATE `tabChat Conversation`
                SET last_message_preview = %s,
                    unread_count = COALESCE(unread_count, 0) + 1,
                    modified = NOW(6),
                    modified_by = %s
                WHERE name = %s
                """,
                ((preview or "")[:500], frappe.session.user, conversation_name),
            ),
        )
        return

    with_db_lock_retry(
        "conversation_preview_update",
        lambda: frappe.db.set_value(
            "Chat Conversation",
            conversation_name,
            {"last_message_preview": (preview or "")[:500]},
            update_modified=True,
        ),
    )


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
    with_db_lock_retry(
        "conversation_mark_read",
        lambda: frappe.db.sql(
            """
            UPDATE `tabChat Conversation`
            SET unread_count = 0,
                modified = NOW(6),
                modified_by = %s
            WHERE name = %s
              AND COALESCE(unread_count, 0) != 0
            """,
            (frappe.session.user, conversation_name),
        ),
    )
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


def _coerce_inbound_raw_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = payload.get("raw_payload")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            import json

            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _link_or_create_master_record(
    conversation: str,
    contact_name: str,
    phone_number: str,
    display_name: Optional[str] = None,
    *,
    raw_payload: Optional[Dict[str, Any]] = None,
    message_name: Optional[str] = None,
) -> None:
    """Attach inbound chat to existing Patient/Customer else create a Lead."""
    if not phone_number:
        return

    convo = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", contact_name)
    _sanitize_contact_links(contact)
    _sanitize_conversation_links(convo)
    _normalize_existing_lead_link(convo)
    existing_crm_lead = get_conversation_crm_lead(convo)
    if existing_crm_lead and frappe.db.exists("CRM Lead", existing_crm_lead):
        _finalize_crm_lead_after_inbound(
            conversation,
            existing_crm_lead,
            raw_payload=raw_payload,
            message_name=message_name,
        )
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
        try:
            from wa_chat_hub.interakt.contact_sync import enqueue_push_for_conversation

            enqueue_push_for_conversation(conversation)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Interakt Contact Push Enqueue Failed")
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

    existing_lead = _find_existing_lead_by_phone(phone_number)
    if existing_lead:
        lead_doctype, lead_name = existing_lead
    else:
        lead_doctype = _preferred_lead_doctype()
        if not lead_doctype:
            return
        lead_name = _create_lead_for_inbound(
            doctype=lead_doctype,
            phone_number=phone_number,
            display_name=display_name or contact.display_name,
            channel_account=convo.channel_account,
        )
    if not lead_name:
        return

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

    if lead_doctype == "CRM Lead":
        _finalize_crm_lead_after_inbound(
            conversation,
            lead_name,
            raw_payload=raw_payload,
            message_name=message_name,
        )

    try:
        from wa_chat_hub.interakt.contact_sync import enqueue_push_for_conversation

        enqueue_push_for_conversation(conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Interakt Contact Push Enqueue Failed")


def _finalize_crm_lead_after_inbound(
    conversation: str,
    lead_name: str,
    *,
    raw_payload: Optional[Dict[str, Any]] = None,
    message_name: Optional[str] = None,
) -> None:
    """Ad attribution → CRM Lead meta tab; lead scoring/OCR fields after link exists."""
    convo = frappe.get_cached_doc("Chat Conversation", conversation)
    _sync_crm_lead_pipeline_for_channel(lead_name, getattr(convo, "channel_account", None))
    try:
        from wa_chat_hub.messaging.crm_lead_meta import sync_crm_lead_meta_from_conversation

        sync_crm_lead_meta_from_conversation(convo, raw_payload=raw_payload, force=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "CRM Lead Meta Sync On Link Failed")

    try:
        from wa_chat_hub.lead_ai import auto_update_lead_from_conversation

        auto_update_lead_from_conversation(lead_name, conversation=conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Lead AI Auto Update Failed")


def _sync_crm_lead_pipeline_for_channel(lead_name: str, channel_account: Optional[str]) -> None:
    """Keep inbound CRM Lead pipeline aligned with the Interakt account that received the chat."""
    if not lead_name or not channel_account or not frappe.db.exists("CRM Lead", lead_name):
        return

    pipeline_fieldname = _get_lead_pipeline_fieldname("CRM Lead")
    if not pipeline_fieldname:
        return

    pipeline = _default_sr_lead_pipeline_for_channel(channel_account)
    if not pipeline:
        return

    current = frappe.db.get_value("CRM Lead", lead_name, pipeline_fieldname)
    if current == pipeline:
        return

    try:
        with _crm_lead_field_guard_bypass(True):
            frappe.db.set_value("CRM Lead", lead_name, pipeline_fieldname, pipeline, update_modified=True)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub CRM Lead Pipeline Sync Failed")


def _inbound_lead_first_name(display_name: Optional[str], phone_number: str) -> str:
    text = (display_name or "").strip()
    if text and text != phone_number:
        return text.split()[0][:140]
    return phone_number[-10:] if len(phone_number) >= 10 else phone_number


def _default_sr_lead_pipeline_for_channel(channel_account: Optional[str]) -> Optional[str]:
    """One SR Lead Pipeline per Interakt Chat Channel Account (WA Channel Pipeline Map)."""
    from wa_chat_hub.messaging.channel_map import get_pipeline_for_channel_account

    return get_pipeline_for_channel_account(channel_account)


def _default_sr_lead_source_for_channel(channel_account: Optional[str], meta) -> Optional[str]:
    """Optional SR Lead Source per Interakt account; never block lead creation."""
    if not channel_account:
        return None

    source_df = meta.get_field("source")
    if not source_df:
        return None

    try:
        from wa_chat_hub.messaging.channel_map import get_source_for_channel_account

        mapped_source = get_source_for_channel_account(channel_account)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Channel Source Mapping Failed")
        return None

    if not mapped_source:
        return None

    if source_df.fieldtype == "Link" and source_df.options:
        return _resolve_or_create_link_value(source_df.options, mapped_source)

    if source_df.fieldtype == "Select":
        options = [opt.strip() for opt in str(source_df.options or "").split("\n") if opt.strip()]
        return mapped_source if mapped_source in options else None

    return mapped_source


def _default_crm_lead_status() -> Optional[str]:
    if not frappe.db.exists("DocType", "CRM Lead Status"):
        return None

    for status in ("Fresh", "New"):
        if frappe.db.exists("CRM Lead Status", status):
            return status

    rows = frappe.get_all(
        "CRM Lead Status",
        pluck="name",
        order_by="position asc, modified asc",
        limit=1,
    )
    return rows[0] if rows else None


def _create_lead_for_inbound(
    doctype: str,
    phone_number: str,
    display_name: Optional[str],
    channel_account: Optional[str] = None,
) -> Optional[str]:
    payload: Dict[str, Any] = {"doctype": doctype}
    context = {
        "lead_doctype": doctype,
        "phone_number": phone_number,
        "display_name": display_name,
        "channel_account": channel_account,
        "source_event": "Inbound WhatsApp",
    }
    try:
        meta = frappe.get_meta(doctype)
        lead_title = display_name or phone_number
        first_name = _inbound_lead_first_name(display_name, phone_number)

        if meta.has_field("first_name"):
            payload["first_name"] = first_name
        if meta.has_field("lead_name"):
            payload["lead_name"] = lead_title
        if meta.has_field("mobile_no"):
            payload["mobile_no"] = phone_number
        elif meta.has_field("phone"):
            payload["phone"] = phone_number
        if meta.has_field("source"):
            source_value = _default_sr_lead_source_for_channel(channel_account, meta) or _resolve_whatsapp_source_value(meta)
            if source_value:
                payload["source"] = source_value
        if meta.has_field("sr_lead_platform"):
            platform_value = _resolve_whatsapp_platform_value(meta)
            if platform_value:
                payload["sr_lead_platform"] = platform_value

        if doctype == "CRM Lead":
            if meta.has_field("status") and not payload.get("status"):
                payload["status"] = _default_crm_lead_status()

        pipeline_fieldname = _get_lead_pipeline_fieldname(doctype)
        pipeline = _default_sr_lead_pipeline_for_channel(channel_account)
        context["pipeline_fieldname"] = pipeline_fieldname
        context["resolved_pipeline"] = pipeline
        if pipeline_fieldname:
            if pipeline:
                payload[pipeline_fieldname] = pipeline
            elif meta.get_field(pipeline_fieldname) and meta.get_field(pipeline_fieldname).reqd:
                frappe.log_error(
                    _(
                        "Skipped CRM Lead for WhatsApp {0}: no WA Channel Pipeline Map for Interakt account {1}. "
                        "Add one active row on WA Channel Pipeline Map with the default SR Lead Pipeline for that account."
                    ).format(phone_number, channel_account or _("(unknown)"))
                    + _json_block(WA_LEAD_CONTEXT_MARKER, context)
                    + _json_block(WA_LEAD_PAYLOAD_MARKER, payload),
                    "WA Chat Hub Inbound Lead Skipped",
                )
                return None
        elif pipeline:
            frappe.log_error(
                f"Default SR Lead Pipeline '{pipeline}' for {channel_account}, but {doctype} has no pipeline Link field."
                + _json_block(WA_LEAD_CONTEXT_MARKER, context)
                + _json_block(WA_LEAD_PAYLOAD_MARKER, payload),
                "WA Channel Pipeline Mapping",
            )

        doc = frappe.get_doc(payload)
        with _crm_lead_field_guard_bypass(doctype == "CRM Lead"):
            doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(
            _lead_creation_error_details(frappe.get_traceback(), payload, context),
            "WA Chat Hub Inbound Lead Create Failed",
        )
        return None


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


def _find_existing_lead_by_phone(phone_number: str) -> Optional[tuple[str, str]]:
    for doctype in ("CRM Lead", "Lead"):
        if not frappe.db.exists("DocType", doctype):
            continue
        if doctype == "CRM Lead":
            found = _find_primary_crm_lead_by_phone(phone_number)
        else:
            found = _find_by_phone(doctype, ["mobile_no", "phone", "custom_whatsapp_number"], phone_number)
        if found:
            return doctype, found
    return None


def _find_primary_crm_lead_by_phone(phone_number: str) -> Optional[str]:
    try:
        from crm_lead_dedupe.leads.dup_utils import get_primary_lead_name_for_mobile, get_primary_lead_name_for_lead, norm_mobile

        primary = get_primary_lead_name_for_mobile(norm_mobile(phone_number))
        if primary:
            return primary

        found = _find_by_phone("CRM Lead", ["mobile_no", "phone", "custom_whatsapp_number"], phone_number)
        return get_primary_lead_name_for_lead(found) if found else None
    except Exception:
        found = _find_by_phone("CRM Lead", ["mobile_no", "phone", "custom_whatsapp_number"], phone_number)
        if found and frappe.db.has_column("CRM Lead", "sr_duplicate_of_name"):
            primary = frappe.db.get_value("CRM Lead", found, "sr_duplicate_of_name")
            if primary and frappe.db.exists("CRM Lead", primary):
                return primary
        return found


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


def _resolve_whatsapp_platform_value(meta) -> Optional[str]:
    """Return/create a safe WhatsApp platform value for CRM Lead when required."""
    platform_df = meta.get_field("sr_lead_platform")
    if not platform_df:
        return None

    if platform_df.fieldtype == "Link" and platform_df.options:
        return _resolve_or_create_link_value(platform_df.options, "WhatsApp")

    if platform_df.fieldtype == "Select":
        options = [opt.strip() for opt in str(platform_df.options or "").split("\n") if opt.strip()]
        return "WhatsApp" if "WhatsApp" in options else None

    return "WhatsApp"


def _resolve_or_create_link_value(doctype: str, value: str) -> Optional[str]:
    """Resolve/create a Link option by docname or title field, returning the real docname."""
    try:
        meta = frappe.get_meta(doctype)
        if frappe.db.exists(doctype, value):
            return value

        title_field = _link_title_field(meta)
        if title_field:
            existing = frappe.db.get_value(doctype, {title_field: value}, "name")
            if existing:
                return existing

        payload: Dict[str, Any] = {"doctype": doctype}
        autoname = str(getattr(meta, "autoname", "") or "")
        if autoname.startswith("field:"):
            fieldname = autoname.split(":", 1)[1]
            payload[fieldname] = value
        elif title_field:
            payload[title_field] = value
        else:
            payload["name"] = value

        if meta.has_field("is_active"):
            payload["is_active"] = 1

        doc = frappe.get_doc(payload)
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"WA Chat Hub Create {doctype} Failed")
        return None


def _link_title_field(meta) -> Optional[str]:
    """Best field to store a human label for simple linked masters."""
    candidates = [
        str(getattr(meta, "title_field", "") or "").strip(),
        "sr_platform_name",
        "platform_name",
        "title",
        "name1",
    ]
    for fieldname in candidates:
        if fieldname and meta.has_field(fieldname):
            return fieldname
    return None


def preserve_remote_file_url_for_frappe(file_url: str) -> str:
    """Protect signed remote URLs from Frappe File.validate() unquoting once."""
    url = str(file_url or "").strip()
    if not _is_remote_url(url):
        return url
    return url.replace("%", "%25")


def _is_remote_url(value: str) -> bool:
    try:
        return urlsplit(str(value or "")).scheme in {"http", "https"}
    except Exception:
        return False


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
        existing_file = frappe.get_doc("File", existing)
        _repair_remote_attachment_comment_url(
            message_doc.doctype,
            message_doc.name,
            existing_file.file_name,
            existing_file.file_url,
        )
        if frappe.get_meta("Chat Message").has_field("attachment_file"):
            frappe.db.set_value("Chat Message", message_doc.name, "attachment_file", existing, update_modified=False)
        return existing

    file_doc = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": filename,
            "file_url": preserve_remote_file_url_for_frappe(media_url),
            "is_private": 0,
            "attached_to_doctype": "Chat Message",
            "attached_to_name": message_doc.name,
        }
    )
    file_doc.insert(ignore_permissions=True)
    _repair_remote_attachment_comment_url(message_doc.doctype, message_doc.name, file_doc.file_name, file_doc.file_url)
    if frappe.get_meta("Chat Message").has_field("attachment_file"):
        frappe.db.set_value("Chat Message", message_doc.name, "attachment_file", file_doc.name, update_modified=False)
    return file_doc.name


def _sync_inbound_attachment_to_linked_record(
    conversation: str,
    chat_file_name: str,
    message_name: str,
    payload: Dict[str, Any],
) -> None:
    """Mirror inbound chat media URL on linked CRM Lead without local/S3 copy."""
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
    existing_lead_file = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
            "file_name": lead_filename,
        },
        "name",
    )
    if existing_lead_file:
        lead_file_doc = frappe.get_doc("File", existing_lead_file)
        _repair_remote_attachment_comment_url(ref_doctype, ref_name, lead_file_doc.file_name, lead_file_doc.file_url)
        return
    existing_lead_file = frappe.db.get_value(
        "File",
        {
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
            "file_url": chat_file.file_url,
        },
        "name",
    )
    if existing_lead_file:
        lead_file_doc = frappe.get_doc("File", existing_lead_file)
        _repair_remote_attachment_comment_url(ref_doctype, ref_name, lead_file_doc.file_name, lead_file_doc.file_url)
        return
    lead_file = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": lead_filename,
            "file_url": preserve_remote_file_url_for_frappe(chat_file.file_url or media_url),
            "is_private": 0,
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
        }
    )
    lead_file.insert(ignore_permissions=True)
    _repair_remote_attachment_comment_url(ref_doctype, ref_name, lead_file.file_name, lead_file.file_url)


def _repair_remote_attachment_comment_url(
    attached_to_doctype: str,
    attached_to_name: str,
    file_name: str,
    file_url: str,
) -> None:
    if not _is_remote_url(file_url):
        return

    comments = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": attached_to_doctype,
            "reference_name": attached_to_name,
            "comment_type": "Attachment",
        },
        fields=["name", "content"],
        order_by="creation desc",
        limit_page_length=5,
    )
    for comment in comments:
        content = str(comment.content or "")
        if file_name and file_name not in content:
            continue
        if "href=" not in content:
            continue
        repaired = _replace_first_href(content, file_url)
        if repaired != content:
            frappe.db.set_value("Comment", comment.name, "content", repaired, update_modified=False)
            return


def _replace_first_href(html: str, url: str) -> str:
    quote_char = "'" if "href='" in html else '"' if 'href="' in html else ""
    if not quote_char:
        return html
    marker = f"href={quote_char}"
    start = html.find(marker)
    if start < 0:
        return html
    value_start = start + len(marker)
    value_end = html.find(quote_char, value_start)
    if value_end < 0:
        return html
    return f"{html[:value_start]}{url}{html[value_end:]}"
