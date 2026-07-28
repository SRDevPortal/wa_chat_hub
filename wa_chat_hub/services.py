from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import frappe
from frappe import _
from frappe.utils import cint, now_datetime
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from wa_chat_hub.ai.ocr_summary import build_attachment_filename, process_attachment_for_lead_summary
from wa_chat_hub.ai.media_transcription import (
    TRANSCRIPT_CONTENT_TYPES,
    process_transcript_for_lead_summary,
)
from wa_chat_hub.db_retry import is_db_lock_conflict, with_db_lock_retry
from wa_chat_hub.messaging.idempotency import (
    build_message_dedupe_key,
    webhook_idempotency_enabled,
)
from wa_chat_hub.phone_normalization import canonical_phone as _canonical_phone
from wa_chat_hub.phone_normalization import normalize_phone
from wa_chat_hub.performance_flags import conversation_job_enqueue_options
from wa_chat_hub.prompts import (
    get_conversation_crm_lead,
    get_conversation_linked_reference,
    set_conversation_crm_lead,
)
from wa_chat_hub.security import (
    WAChatHubSecurityError,
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_get_value,
    safe_ai_insert,
    safe_ai_set_value,
)
from wa_chat_hub.task_logger import elapsed, task_log


DEFAULT_CONVERSATION_STATUS = "Open"
ACTIVE_CONVERSATION_STATUSES = ("Open", "Pending", "Resolved")
WA_LEAD_CONTEXT_MARKER = "WA_CHAT_HUB_CONTEXT_JSON"
WA_LEAD_PAYLOAD_MARKER = "WA_CHAT_HUB_PAYLOAD_JSON"
APPEND_MESSAGE_LOCK_TIMEOUT = 8
CONVERSATION_UPDATE_LOCK_TIMEOUT = 8
FILE_LOCK_RETRY_ATTEMPTS = 3
FILE_LOCK_RETRY_DELAY_SECONDS = 0.35
CONTACT_DUPLICATE_VISIBILITY_ATTEMPTS = 20
CONTACT_DUPLICATE_VISIBILITY_DELAY_SECONDS = 0.25
PHONE_INDEX_FIELD_BY_SOURCE = {
    "mobile": "vobiz_mobile_last10",
    "mobile_no": "vobiz_mobile_last10",
    "phone": "vobiz_phone_last10",
    "custom_whatsapp_number": "vobiz_whatsapp_last10",
}
PHONE_CANONICAL_INDEX_FIELDS = ("vobiz_normalized_phone", "sr_mobile_norm")


def _indexed_phone_lookup_enabled() -> bool:
    """Keep legacy lookup available until normalized-key coverage is validated."""
    try:
        settings = frappe.get_cached_doc("WA Chat Hub Settings")
        if not settings.meta.has_field("enable_indexed_phone_lookup"):
            return False
        return bool(cint(settings.enable_indexed_phone_lookup))
    except Exception:
        return False


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


def _record_lock_name(prefix: str, token: str) -> str:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    return f"wa_chat_{prefix}_{digest}"


def _append_message_lock_name(payload: Dict[str, Any]) -> str:
    channel_account = str(payload.get("channel_account") or "unknown").strip()
    phone_number = normalize_phone(payload.get("phone_number") or payload.get("to") or payload.get("from"))
    token = f"{channel_account}:{phone_number or payload.get('conversation') or 'unknown'}"
    return _record_lock_name("append", token)


@contextmanager
def conversation_update_lock(conversation: str):
    with filelock(
        _record_lock_name("conversation", str(conversation or "unknown")),
        timeout=CONVERSATION_UPDATE_LOCK_TIMEOUT,
    ):
        yield


def _is_file_lock_timeout(exc: Exception) -> bool:
    return isinstance(exc, LockTimeoutError)


def _is_duplicate_entry(exc: Exception) -> bool:
    return isinstance(exc, frappe.DuplicateEntryError) or exc.__class__.__name__ == "DuplicateEntryError"


def _sleep_before_file_lock_retry(attempt: int) -> None:
    time.sleep(FILE_LOCK_RETRY_DELAY_SECONDS * attempt)


def _run_with_file_lock_retry(label: str, action):
    for attempt in range(1, FILE_LOCK_RETRY_ATTEMPTS + 1):
        try:
            return action()
        except Exception as exc:
            if not _is_file_lock_timeout(exc) or attempt >= FILE_LOCK_RETRY_ATTEMPTS:
                raise
            task_log(
                "file_lock",
                "retry",
                label=label,
                attempt=attempt,
                error=str(exc)[:140],
            )
            _sleep_before_file_lock_retry(attempt)

    raise RuntimeError(f"File lock retry exhausted for {label}")


def _valid_link(doctype: str, value: Optional[str]) -> Optional[str]:
    """Return value only if it exists in the linked DocType (avoids webhook hard-fail)."""
    name = (value or "").strip()
    if not name:
        return None
    try:
        if safe_ai_exists(doctype, name):
            return name
    except WAChatHubSecurityError:
        return None
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

    try:
        rows = safe_ai_get_all(
            "Chat Assignment Rule",
            filters=filters,
            fields=["assign_to"],
            limit=1,
        )
    except WAChatHubSecurityError:
        return None
    return rows[0].assign_to if rows else None


def get_or_create_contact(phone_number: str, display_name: Optional[str] = None) -> str:
    normalized = normalize_phone(phone_number)
    existing = safe_ai_get_value("Chat Contact", {"phone_number": normalized}, "name")
    if existing:
        if display_name and safe_ai_get_value("Chat Contact", existing, "display_name") != display_name:
            with_db_lock_retry(
                "contact_display_name_update",
                lambda: safe_ai_set_value(
                    "Chat Contact",
                    existing,
                    "display_name",
                    display_name,
                    update_modified=False,
                ),
            )
        return existing

    doc = frappe.get_doc({
        "doctype": "Chat Contact",
        "phone_number": normalized,
        "display_name": display_name or normalized,
    })
    try:
        safe_ai_insert(doc)
    except Exception as exc:
        if not _is_duplicate_entry(exc):
            raise
        existing = _wait_for_duplicate_contact(normalized)
        if not existing:
            raise
        if display_name and safe_ai_exists("Chat Contact", existing):
            with_db_lock_retry(
                "contact_display_name_update",
                lambda: safe_ai_set_value(
                    "Chat Contact",
                    existing,
                    "display_name",
                    display_name,
                    update_modified=False,
                ),
            )
        return existing
    return doc.name


def _wait_for_duplicate_contact(phone_number: str) -> Optional[str]:
    for attempt in range(CONTACT_DUPLICATE_VISIBILITY_ATTEMPTS):
        existing = safe_ai_get_value("Chat Contact", {"phone_number": phone_number}, "name")
        if existing:
            return existing
        time.sleep(CONTACT_DUPLICATE_VISIBILITY_DELAY_SECONDS)
    return None


def get_or_create_conversation(
    channel_account: str,
    contact: str,
    department: Optional[str] = None,
    assigned_to: Optional[str] = None,
    status: str = DEFAULT_CONVERSATION_STATUS,
) -> str:
    filters = {
        "channel_account": channel_account,
        "contact": contact,
        "status": ["in", ACTIVE_CONVERSATION_STATUSES],
    }

    existing = with_db_lock_retry(
        "conversation_lookup",
        lambda: safe_ai_get_value("Chat Conversation", filters, "name"),
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
                lambda: safe_ai_set_value(
                    "Chat Conversation",
                    existing,
                    updates,
                    update_modified=False,
                ),
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
            safe_ai_insert(doc)
            return doc.name
        except Exception as exc:
            if not is_db_lock_conflict(exc):
                raise
            concurrent = safe_ai_get_value("Chat Conversation", filters, "name")
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
        result = _run_with_file_lock_retry(
            "append_message",
            lambda: _append_message_with_lock(payload),
        )
        if not result.get("duplicate"):
            _run_append_message_followups(payload, result)
        task_log(
            "message",
            "append_done",
            direction=payload.get("direction", "Inbound"),
            conversation=result.get("conversation"),
            message=result.get("message"),
            duration_sec=elapsed(started),
        )
        return _append_message_public_result(result)
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


def _append_message_with_lock(payload: Dict[str, Any]) -> Dict[str, str]:
    with filelock(_append_message_lock_name(payload), timeout=APPEND_MESSAGE_LOCK_TIMEOUT):
        result = _append_message_impl(payload)
        # Make the core append visible before releasing the per-chat lock.
        # Follow-up work runs after this lock, and concurrent webhooks for the same
        # new contact must be able to see the committed Chat Contact/Conversation.
        frappe.db.commit()
        return result


def _append_message_public_result(result: Dict[str, str]) -> Dict[str, str]:
    return {
        "contact": result.get("contact"),
        "conversation": result.get("conversation"),
        "message": result.get("message"),
        "duplicate": bool(result.get("duplicate")),
    }


def _append_message_impl(payload: Dict[str, Any]) -> Dict[str, str]:
    phone_number = normalize_phone(payload.get("phone_number") or payload.get("to") or payload.get("from"))
    if not phone_number:
        frappe.throw(_("Cannot store WhatsApp message: customer phone number is missing in webhook payload."))
    contact = get_or_create_contact(phone_number=phone_number, display_name=payload.get("display_name"))

    channel_account = payload["channel_account"]
    existing_conversation = safe_ai_get_value(
        "Chat Conversation",
        {
            "channel_account": channel_account,
            "contact": contact,
            "status": ["in", ACTIVE_CONVERSATION_STATUSES],
        },
        "name",
    )

    # Preserve existing conversations: map defaults apply only when creating a new thread.
    # Chat Conversation.department → ERPNext "Department", not Medical Department.
    # sr_medical_department on WA Channel Pipeline Map is only for Patient routing / Interakt traits.
    channel_department = _valid_link("Department", payload.get("channel_department"))
    if not channel_department and not existing_conversation:
        account_department = safe_ai_get_value(
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
    provider_name = str(
        payload.get("provider_name") or payload.get("provider") or payload.get("channel_type") or "unknown"
    ).strip()
    provider_message_id = payload.get("provider_message_id")
    channel_message_id = payload.get("channel_message_id")
    provider_event_id = payload.get("provider_event_id")
    explicit_dedupe_key = None
    if direction == "Outbound" and payload.get("sender_type") == "System":
        explicit_dedupe_key = str(payload.get("dedupe_key") or "").strip() or None
    dedupe_key = explicit_dedupe_key
    if not dedupe_key and webhook_idempotency_enabled():
        dedupe_key = build_message_dedupe_key(
            conversation=conversation,
            provider_name=provider_name,
            provider_message_id=provider_message_id,
            channel_message_id=channel_message_id,
            provider_event_id=provider_event_id,
        )
    if dedupe_key:
        existing_message = safe_ai_get_value("Chat Message", {"dedupe_key": dedupe_key}, "name")
        if existing_message:
            task_log(
                "message",
                "append_duplicate",
                conversation=conversation,
                message=existing_message,
                provider=provider_name,
            )
            return {
                "contact": contact,
                "conversation": conversation,
                "message": existing_message,
                "phone_number": phone_number,
                "direction": direction,
                "duplicate": True,
            }

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
        "attachment_file": payload.get("attachment_file"),
        "channel_message_id": channel_message_id,
        "provider_message_id": provider_message_id,
        "provider_name": provider_name,
        "provider_event_id": provider_event_id,
        "dedupe_key": dedupe_key,
        "delivery_status": delivery_status,
        "raw_payload": frappe.as_json(payload),
        "raw_transport_payload": frappe.as_json(payload.get("raw_transport_payload") or {}),
    })

    savepoint = "wa_chat_message_insert"
    frappe.db.savepoint(savepoint)
    try:
        # Open the window before insert so after_insert automation observes active state.
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

        safe_ai_insert(message)
        frappe.db.release_savepoint(savepoint)
    except Exception as exc:
        if not dedupe_key or not _is_duplicate_entry(exc):
            raise
        frappe.db.rollback(save_point=savepoint)
        existing_message = safe_ai_get_value("Chat Message", {"dedupe_key": dedupe_key}, "name")
        if not existing_message:
            raise
        task_log(
            "message",
            "append_duplicate_race",
            conversation=conversation,
            message=existing_message,
            provider=provider_name,
        )
        return {
            "contact": contact,
            "conversation": conversation,
            "message": existing_message,
            "phone_number": phone_number,
            "direction": direction,
            "duplicate": True,
        }

    try:
        _run_with_file_lock_retry(
            "conversation_update_after_message",
            lambda: update_conversation_after_message(conversation, payload),
        )
    except LockTimeoutError as exc:
        task_log(
            "message",
            "conversation_update_lock_timeout",
            conversation=conversation,
            message=message.name,
            direction=direction,
            error=str(exc)[:140],
        )
    return {
        "contact": contact,
        "conversation": conversation,
        "message": message.name,
        "phone_number": phone_number,
        "direction": direction,
    }


def _run_append_message_followups(payload: Dict[str, Any], result: Dict[str, str]) -> None:
    conversation = result.get("conversation")
    contact = result.get("contact")
    message_name = result.get("message")
    phone_number = result.get("phone_number") or normalize_phone(
        payload.get("phone_number") or payload.get("to") or payload.get("from")
    )
    direction = result.get("direction") or payload.get("direction", "Inbound")

    message = None
    if message_name:
        try:
            message = safe_ai_get_doc("Chat Message", message_name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA Chat Hub Message Reload Failed")

    if message and payload.get("attachment_file"):
        try:
            _attach_outbound_file_to_message(message, str(payload.get("attachment_file")))
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Outbound Attachment Link Failed")

    if direction == "Inbound" and conversation and contact:
        try:
            _link_or_create_master_record(
                conversation=conversation,
                contact_name=contact,
                phone_number=phone_number,
                display_name=payload.get("display_name"),
                raw_payload=_coerce_inbound_raw_payload(payload),
                message_name=message_name,
            )
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "WA Chat Hub Inbound Link Failed",
            )

    if direction == "Inbound" and conversation and message_name:
        try:
            from wa_chat_hub.identity import verify_patient_identity_from_inbound_message

            verify_patient_identity_from_inbound_message(conversation, message_name)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                "WA Chat Hub Patient Verification Failed",
            )

    attachment_file = None
    if message:
        try:
            attachment_file = _persist_inbound_attachment(message, payload)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Inbound Attachment Persistence Failed")

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
                message_name=message_name,
                payload=payload,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "CRM Lead Attachment Sync Failed")
        content_type = str(payload.get("content_type") or "").title()
        try:
            _enqueue_inbound_media_lead_summary(
                conversation=conversation,
                message_name=message_name,
                payload=payload,
                content_type=content_type,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Media Lead Summary Enqueue Failed")
    if payload.get("attachment_file") and direction == "Outbound" and message_name:
        try:
            _sync_outbound_attachment_to_linked_record(
                conversation=conversation,
                chat_file_name=str(payload.get("attachment_file")),
                message_name=message_name,
                payload=payload,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "CRM Lead Outbound Attachment Sync Failed")
    if direction == "Inbound" and not (
        getattr(frappe.flags, "wa_ai_outbound_reply", False)
        or getattr(frappe.local, "wa_ai_outbound_reply", False)
    ):
        try:
            from wa_chat_hub.api.ai_bot import schedule_autopilot_for_message

            schedule_autopilot_for_message(message_name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Autopilot Schedule Failed")
    if message:
        frappe.publish_realtime(
            "wa_chat_new_message",
            {
                "conversation": conversation,
                "message": message.as_dict(),
                "direction": message.direction,
            },
            after_commit=True,
        )


def _enqueue_lead_scoring(conversation: str) -> None:
    """Score after commit so inbound webhooks are not held by lead/LLM work."""
    if not conversation:
        return

    enqueue_options = conversation_job_enqueue_options(
        f"wa_lead_score_{conversation}"
    )
    frappe.enqueue(
        "wa_chat_hub.ai.lead_scoring.score_and_sync_conversation",
        queue="short",
        conversation=conversation,
        timeout=90,
        enqueue_after_commit=True,
        now=frappe.flags.in_test,
        **enqueue_options,
    )
    task_log(
        "lead_score",
        "enqueue",
        conversation=conversation,
        queue="short",
        deduplicated=bool(enqueue_options),
    )


def _enqueue_inbound_media_lead_summary(
    conversation: str,
    message_name: str,
    payload: Dict[str, Any],
    content_type: str,
) -> None:
    """Run OCR/transcription lead-note work after commit so replies are not blocked."""
    if content_type in TRANSCRIPT_CONTENT_TYPES:
        method = "wa_chat_hub.services.process_inbound_transcript_lead_summary"
        job_id = f"wa_media_transcript_summary_{message_name}"
    else:
        method = "wa_chat_hub.services.process_inbound_ocr_lead_summary"
        job_id = f"wa_media_ocr_summary_{message_name}"

    frappe.enqueue(
        method,
        queue="long",
        conversation=conversation,
        message_name=message_name,
        payload=payload,
        timeout=300,
        enqueue_after_commit=True,
        now=frappe.flags.in_test,
        job_id=job_id,
        deduplicate=True,
    )
    task_log("media_summary", "enqueue", conversation=conversation, message=message_name, queue="long")


def process_inbound_ocr_lead_summary(conversation: str, message_name: str, payload: Dict[str, Any]) -> None:
    try:
        process_attachment_for_lead_summary(conversation, message_name, payload)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "OCR Lead Summary Failed")


def process_inbound_transcript_lead_summary(
    conversation: str,
    message_name: str,
    payload: Dict[str, Any],
) -> None:
    try:
        process_transcript_for_lead_summary(conversation, message_name, payload)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Media Transcript Lead Summary Failed")


def cint_safe(value: Any) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def update_conversation_after_message(conversation_name: str, payload: Dict[str, Any]) -> None:
    """Update preview/unread without full doc save (avoids TimestampMismatch under concurrent updates)."""
    with conversation_update_lock(conversation_name):
        body = payload.get("body")
        content_type = payload.get("content_type") or "Text"
        media_url = payload.get("media_url")
        assert_ai_doctype_permission("Chat Conversation", "read")
        has_last_message_time = frappe.db.has_column("Chat Conversation", "last_message_time")
        if media_url and content_type != "Text":
            preview = build_media_preview(content_type, body)
        else:
            preview = body or content_type or ""

        if payload.get("direction", "Inbound") == "Inbound":
            last_message_sql = "last_message_time = NOW(6)," if has_last_message_time else ""
            assert_ai_doctype_permission("Chat Conversation", "write")
            with_db_lock_retry(
                "conversation_unread_increment",
                lambda: frappe.db.sql(
                    f"""
                    UPDATE `tabChat Conversation`
                    SET last_message_preview = %s,
                        {last_message_sql}
                        unread_count = COALESCE(unread_count, 0) + 1,
                        modified = NOW(6),
                        modified_by = %s
                    WHERE name = %s
                    """,
                    ((preview or "")[:500], frappe.session.user, conversation_name),
                ),
            )
            return

        values = {"last_message_preview": (preview or "")[:500]}
        if has_last_message_time:
            values["last_message_time"] = frappe.utils.now_datetime()
        with_db_lock_retry(
            "conversation_preview_update",
            lambda: safe_ai_set_value(
                "Chat Conversation",
                conversation_name,
                values,
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
    assert_ai_doctype_permission("Chat Conversation", "write")
    unread_count = cint(frappe.db.get_value("Chat Conversation", conversation_name, "unread_count") or 0)
    if unread_count <= 0:
        return

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
    assert_ai_doctype_permission("Chat Message", "write")
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


def _apply_vobiz_patient_routing(
    conversation: str,
    patient: str,
    channel_account: Optional[str] = None,
) -> None:
    if not conversation or not patient:
        return

    if not frappe.db.exists("DocType", "Vobiz AI Settings"):
        return

    did_number = _channel_account_phone_number(channel_account)
    try:
        from vobiz_ai.api.patient_routing import resolve_patient_routing_for_chat
    except ModuleNotFoundError as exc:
        if str(getattr(exc, "name", "")).startswith("vobiz_ai"):
            task_log(
                "patient_routing",
                "optional_vobiz_module_missing",
                conversation=conversation,
                patient=patient,
                module=getattr(exc, "name", ""),
            )
            return
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Vobiz Patient Routing Failed")
        return

    try:
        routing = resolve_patient_routing_for_chat(patient=patient, did_number=did_number)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Vobiz Patient Routing Failed")
        return

    user = _vobiz_patient_routing_user(routing)
    status = routing.get("status") or ""
    if not user:
        _log_vobiz_patient_routing(
            conversation,
            channel_account,
            patient,
            routing,
            status="Skipped",
            details=f"Vobiz patient routing returned no assignable user. Status: {status or '-'}",
        )
        return

    with conversation_update_lock(conversation):
        existing_assignee = safe_ai_get_value("Chat Conversation", conversation, "assigned_to")
        if existing_assignee:
            _log_vobiz_patient_routing(
                conversation,
                channel_account,
                patient,
                routing,
                status="Skipped",
                details=(
                    "Preserved existing conversation assignment "
                    f"{existing_assignee}; Vobiz routed user was {user}."
                ),
            )
            return

        with_db_lock_retry(
            "vobiz_patient_routing_assignment",
            lambda: safe_ai_set_value(
                "Chat Conversation",
                conversation,
                "assigned_to",
                user,
                update_modified=False,
            ),
        )

    _log_vobiz_patient_routing(
        conversation,
        channel_account,
        patient,
        routing,
        status="Success",
        details=f"Assigned Patient conversation to {user} using Vobiz patient routing.",
    )
    task_log(
        "patient_routing",
        "assigned",
        conversation=conversation,
        patient=patient,
        assigned_to=user,
        routing_status=status,
        routing_group=routing.get("routing_group"),
    )


def _channel_account_phone_number(channel_account: Optional[str]) -> str:
    if not channel_account:
        return ""
    try:
        return safe_ai_get_value("Chat Channel Account", channel_account, "phone_number") or ""
    except Exception:
        return ""


def _vobiz_patient_routing_user(routing: Dict[str, Any]) -> Optional[str]:
    if not routing:
        return None

    status = routing.get("status")
    candidate = ""
    if status == "selected":
        candidate = routing.get("agent_user") or ""
    elif status in {"fallback", "no_available_agent"}:
        candidate = routing.get("fallback_user") or routing.get("agent_user") or ""
    else:
        return None

    return _valid_link("User", candidate)


def _skip_vobiz_patient_routing_for_ambiguous_match(
    conversation: str,
    patient: str,
    channel_account: Optional[str],
    phone_number: str,
) -> bool:
    fields = ["mobile", "mobile_no", "phone", "custom_whatsapp_number"]
    matches = _find_phone_match_names("Patient", fields, phone_number)
    if len(matches) <= 1:
        return False

    _log_vobiz_patient_routing(
        conversation,
        channel_account,
        patient,
        {
            "success": True,
            "enabled": True,
            "matched": False,
            "patient": patient,
            "status": "ambiguous_patient_match",
            "matched_patients": sorted(matches),
        },
        status="Skipped",
        details=(
            "Skipped Vobiz patient routing because the inbound phone matched "
            f"multiple Patient records: {', '.join(sorted(matches))}."
        ),
    )
    return True


def _log_vobiz_patient_routing(
    conversation: str,
    channel_account: Optional[str],
    patient: str,
    routing: Dict[str, Any],
    *,
    status: str,
    details: str,
) -> None:
    try:
        from wa_chat_hub.wa_chat_hub.doctype.chat_action_log.chat_action_log import log_chat_action

        log_chat_action(
            "Assignment",
            "Vobiz Patient Routing",
            status=status,
            conversation=conversation,
            channel_account=channel_account,
            action_source="System",
            reference_doctype="Patient",
            reference_name=patient,
            details=details,
            request_json=json.dumps(
                {
                    "patient": patient,
                    "did_number": _channel_account_phone_number(channel_account),
                },
                ensure_ascii=True,
                default=str,
            ),
            response_json=json.dumps(routing or {}, ensure_ascii=True, default=str),
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Vobiz Patient Routing Log Failed")


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

    convo = safe_ai_get_doc("Chat Conversation", conversation)
    contact = safe_ai_get_doc("Chat Contact", contact_name)
    _sanitize_contact_links(contact)
    _sanitize_conversation_links(convo)
    _normalize_existing_lead_link(convo)
    try:
        from wa_chat_hub.identity import reconcile_conversation_identity

        reconcile_conversation_identity(conversation, source="inbound")
        convo.reload()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Identity Reconciliation Failed")

    ref_dt, ref_name = get_conversation_linked_reference(convo)
    if ref_dt == "Patient" and ref_name:
        _apply_vobiz_patient_routing(conversation, ref_name, getattr(convo, "channel_account", None))
        return

    patient_matches = _find_indexed_phone_match_names(
        "Patient",
        ["mobile", "mobile_no", "phone", "custom_whatsapp_number"],
        phone_number,
        limit=2,
    )
    if len(patient_matches) > 1:
        _mark_conversation_patient_ambiguous(convo)
        return
    if patient_matches:
        _link_patient_to_conversation(
            conversation=conversation,
            convo=convo,
            contact=contact,
            patient_name=next(iter(patient_matches)),
            phone_number=phone_number,
            display_name=display_name,
        )
        return

    existing_crm_lead = get_conversation_crm_lead(convo)
    if existing_crm_lead and safe_ai_exists("CRM Lead", existing_crm_lead):
        _finalize_crm_lead_after_inbound(
            conversation,
            existing_crm_lead,
            raw_payload=raw_payload,
            message_name=message_name,
        )
        return
    if ref_dt and ref_name and ref_dt not in {"CRM Lead", "Lead"}:
        return

    customer_name = _find_by_phone("Customer", ["mobile_no", "phone", "custom_whatsapp_number"], phone_number)
    if customer_name:
        contact_updates = {
            "source_doctype": "Customer",
            "source_name": customer_name,
        }
        if display_name and not contact.display_name:
            contact_updates["display_name"] = display_name
        _set_contact_fields(contact, contact_updates)
        _set_conversation_fields(
            convo,
            {
                "linked_reference_doctype": "Customer",
                "linked_reference_name": customer_name,
            },
        )
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

    contact_updates = {
        "linked_lead": lead_name if lead_doctype == "Lead" else None,
        "source_doctype": lead_doctype,
        "source_name": lead_name,
    }
    if display_name and not contact.display_name:
        contact_updates["display_name"] = display_name
    _set_contact_fields(contact, contact_updates)

    if lead_doctype == "CRM Lead":
        set_conversation_crm_lead(convo, lead_name)
    else:
        convo.linked_reference_doctype = lead_doctype
        convo.linked_reference_name = lead_name
    conversation_updates = {
        "linked_reference_doctype": convo.linked_reference_doctype,
        "linked_reference_name": convo.linked_reference_name,
    }
    assert_ai_doctype_permission("Chat Conversation", "read")
    if frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
        conversation_updates["linked_crm_lead"] = getattr(convo, "linked_crm_lead", None)

    if lead_doctype == "CRM Lead":
        _finalize_crm_lead_after_inbound(
            conversation,
            lead_name,
            raw_payload=raw_payload,
            message_name=message_name,
            convo=convo,
        )

    _set_conversation_fields(convo, conversation_updates)

    try:
        from wa_chat_hub.interakt.contact_sync import enqueue_push_for_conversation

        enqueue_push_for_conversation(conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Interakt Contact Push Enqueue Failed")


def _set_contact_fields(contact, updates: Dict[str, Any]) -> None:
    updates = dict(updates or {})
    if not updates:
        return
    with_db_lock_retry(
        "contact_link_update",
        lambda: safe_ai_set_value(
            "Chat Contact",
            contact.name,
            updates,
            update_modified=False,
        ),
    )
    for key, value in updates.items():
        setattr(contact, key, value)


def _link_patient_to_conversation(
    *,
    conversation: str,
    convo,
    contact,
    patient_name: str,
    phone_number: str,
    display_name: Optional[str] = None,
) -> None:
    """Make a unique Patient authoritative while preserving CRM Lead history."""
    contact_updates = {
        "linked_patient": patient_name,
        "source_doctype": "Patient",
        "source_name": patient_name,
    }
    if display_name and not contact.display_name:
        contact_updates["display_name"] = display_name
    _set_contact_fields(contact, contact_updates)

    try:
        from wa_chat_hub.identity import reconcile_conversation_identity

        reconcile_conversation_identity(
            conversation,
            patient=patient_name,
            crm_lead=getattr(convo, "linked_crm_lead", None),
            source="inbound_phone_match",
        )
        convo.reload()
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "WA Chat Hub Patient Identity Reconciliation Failed",
        )
        return

    _apply_vobiz_patient_routing(
        conversation,
        patient_name,
        getattr(convo, "channel_account", None),
    )
    try:
        from wa_chat_hub.interakt.contact_sync import enqueue_push_for_conversation

        enqueue_push_for_conversation(conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Interakt Contact Push Enqueue Failed")


def _mark_conversation_patient_ambiguous(convo) -> None:
    """A shared phone is Patient traffic, but no record may be selected automatically."""
    meta = frappe.get_meta("Chat Conversation")
    updates = {
        "linked_patient": None,
        "party_type": "Patient",
        "identity_status": "Ambiguous",
        "agent_profile": None,
        "routing_reason": "patient_identity:ambiguous_phone_match",
        "last_identity_sync_at": now_datetime(),
    }
    updates = {key: value for key, value in updates.items() if meta.has_field(key)}
    _set_conversation_fields(convo, updates)


def _set_conversation_fields(convo, updates: Dict[str, Any]) -> None:
    updates = {key: value for key, value in (updates or {}).items()}
    if not updates:
        return
    with_db_lock_retry(
        "conversation_link_update",
        lambda: safe_ai_set_value(
            "Chat Conversation",
            convo.name,
            updates,
            update_modified=False,
        ),
    )
    for key, value in updates.items():
        setattr(convo, key, value)


def _finalize_crm_lead_after_inbound(
    conversation: str,
    lead_name: str,
    *,
    raw_payload: Optional[Dict[str, Any]] = None,
    message_name: Optional[str] = None,
    convo=None,
) -> None:
    """Ad attribution → CRM Lead meta tab; lead scoring/OCR fields after link exists."""
    convo = convo or safe_ai_get_doc("Chat Conversation", conversation)
    _sync_crm_lead_pipeline_for_channel(lead_name, getattr(convo, "channel_account", None))
    try:
        from wa_chat_hub.messaging.crm_lead_meta import sync_crm_lead_meta_from_conversation

        sync_crm_lead_meta_from_conversation(convo, raw_payload=raw_payload, force=True, lead_name=lead_name)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "CRM Lead Meta Sync On Link Failed")

    try:
        from wa_chat_hub.lead_ai import auto_update_lead_from_conversation

        auto_update_lead_from_conversation(lead_name, conversation=conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Lead AI Auto Update Failed")


def _sync_crm_lead_pipeline_for_channel(lead_name: str, channel_account: Optional[str]) -> None:
    """Keep inbound CRM Lead pipeline aligned with the Interakt account that received the chat."""
    if not lead_name or not channel_account or not safe_ai_exists("CRM Lead", lead_name):
        return

    pipeline_fieldname = _get_lead_pipeline_fieldname("CRM Lead")
    if not pipeline_fieldname:
        return

    pipeline = _default_sr_lead_pipeline_for_channel(channel_account)
    if not pipeline:
        return

    current = safe_ai_get_value("CRM Lead", lead_name, pipeline_fieldname)
    if current == pipeline:
        return

    try:
        with _crm_lead_field_guard_bypass(True):
            safe_ai_set_value("CRM Lead", lead_name, pipeline_fieldname, pipeline, update_modified=True)
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
    try:
        if not safe_ai_exists("DocType", "CRM Lead Status"):
            return None

        for status in ("Fresh", "New"):
            if safe_ai_exists("CRM Lead Status", status):
                return status

        rows = safe_ai_get_all(
            "CRM Lead Status",
            pluck="name",
            order_by="position asc, modified asc",
            limit=1,
        )
        return rows[0] if rows else None
    except WAChatHubSecurityError:
        return None


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
        assert_ai_doctype_permission(doctype, "read")
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
            safe_ai_insert(doc)
        return doc.name
    except Exception:
        frappe.log_error(
            _lead_creation_error_details(frappe.get_traceback(), payload, context),
            "WA Chat Hub Inbound Lead Create Failed",
        )
        return None


def _available_phone_index_filters(meta, phone_fields: list[str], phone_number: str) -> list[tuple[str, str]]:
    """Return exact, index-friendly phone predicates in deterministic priority order."""
    normalized = normalize_phone(phone_number)
    canonical = _canonical_phone(phone_number)
    if not normalized:
        return []

    last10 = normalized[-10:] if len(normalized) >= 10 else normalized
    filters: list[tuple[str, str]] = []
    seen = set()

    for fieldname in PHONE_CANONICAL_INDEX_FIELDS:
        if meta.has_field(fieldname) and fieldname not in seen:
            filters.append((fieldname, canonical if fieldname == "vobiz_normalized_phone" else last10))
            seen.add(fieldname)

    for source_field in phone_fields:
        fieldname = PHONE_INDEX_FIELD_BY_SOURCE.get(source_field)
        if fieldname and meta.has_field(fieldname) and fieldname not in seen:
            filters.append((fieldname, last10))
            seen.add(fieldname)

    return filters


def _phone_rows(doctype: str, fieldname: str, value: str, *, limit: int):
    return safe_ai_get_all(
        doctype,
        filters={fieldname: value},
        fields=["name"],
        order_by="modified desc",
        limit_page_length=limit,
    )


def _indexed_phone_rows(doctype: str, fieldname: str, value: str, *, limit: int):
    """Exact bounded lookup; no sort is needed when resolving identity."""
    return safe_ai_get_all(
        doctype,
        filters={fieldname: value},
        fields=["name"],
        limit_page_length=limit,
    )


def _find_indexed_phone_match_names(
    doctype: str,
    phone_fields: list[str],
    phone_number: str,
    *,
    limit: int = 2,
) -> set[str]:
    """Return zero, one, or multiple exact normalized matches without wildcard scans."""
    matches: set[str] = set()
    try:
        if not safe_ai_exists("DocType", doctype):
            return matches

        assert_ai_doctype_permission(doctype, "read")
        meta = frappe.get_meta(doctype)
        normalized = normalize_phone(phone_number)
        if not normalized:
            return matches

        bounded_limit = max(2, min(int(limit or 2), 10))
        for fieldname, value in _available_phone_index_filters(meta, phone_fields, normalized):
            rows = _indexed_phone_rows(
                doctype,
                fieldname,
                value,
                limit=bounded_limit,
            )
            matches.update(row.name for row in rows if row.get("name"))
            if len(matches) >= bounded_limit:
                break
    except WAChatHubSecurityError:
        return set()
    return matches


def _find_by_phone(doctype: str, phone_fields: list[str], phone_number: str) -> Optional[str]:
    try:
        if not safe_ai_exists("DocType", doctype):
            return None

        assert_ai_doctype_permission(doctype, "read")
        meta = frappe.get_meta(doctype)
        normalized = normalize_phone(phone_number)
        if not normalized:
            return None

        # Exact checks are cheap and preserve matches for already-normalized source fields.
        for fieldname in phone_fields:
            if not meta.has_field(fieldname):
                continue
            rows = _phone_rows(doctype, fieldname, normalized, limit=1)
            if rows:
                return rows[0].name

        for fieldname, value in _available_phone_index_filters(meta, phone_fields, normalized):
            rows = _phone_rows(doctype, fieldname, value, limit=1)
            if rows:
                return rows[0].name
    except WAChatHubSecurityError:
        return None
    return None


def _find_phone_match_names(doctype: str, phone_fields: list[str], phone_number: str) -> set[str]:
    matches: set[str] = set()
    try:
        if not safe_ai_exists("DocType", doctype):
            return matches

        assert_ai_doctype_permission(doctype, "read")
        meta = frappe.get_meta(doctype)
        normalized = normalize_phone(phone_number)
        if not normalized:
            return matches

        for fieldname in phone_fields:
            if not meta.has_field(fieldname):
                continue
            exact_rows = _phone_rows(doctype, fieldname, normalized, limit=50)
            matches.update(row.name for row in exact_rows if row.get("name"))

        for fieldname, value in _available_phone_index_filters(meta, phone_fields, normalized):
            rows = _phone_rows(doctype, fieldname, value, limit=50)
            matches.update(row.name for row in rows if row.get("name"))
    except WAChatHubSecurityError:
        return set()
    return matches


def _get_lead_pipeline_fieldname(lead_doctype: str) -> Optional[str]:
    """Auto-detect first Link field on Lead/CRM Lead targeting SR Lead Pipeline."""
    try:
        if not safe_ai_exists("DocType", "SR Lead Pipeline"):
            return None
        assert_ai_doctype_permission(lead_doctype, "read")
        meta = frappe.get_meta(lead_doctype)
        for field in meta.fields:
            if field.fieldtype == "Link" and field.options == "SR Lead Pipeline":
                return field.fieldname
    except WAChatHubSecurityError:
        return None
    return None


def _preferred_lead_doctype() -> Optional[str]:
    try:
        if safe_ai_exists("DocType", "CRM Lead"):
            return "CRM Lead"
        if safe_ai_exists("DocType", "Lead"):
            return "Lead"
    except WAChatHubSecurityError:
        return None
    return None


def _find_existing_lead_by_phone(phone_number: str) -> Optional[tuple[str, str]]:
    for doctype in ("CRM Lead", "Lead"):
        try:
            exists = safe_ai_exists("DocType", doctype)
        except WAChatHubSecurityError:
            continue
        if not exists:
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
        try:
            assert_ai_doctype_permission("CRM Lead", "read")
            if found and frappe.db.has_column("CRM Lead", "sr_duplicate_of_name"):
                primary = safe_ai_get_value("CRM Lead", found, "sr_duplicate_of_name")
                if primary and safe_ai_exists("CRM Lead", primary):
                    return primary
        except WAChatHubSecurityError:
            return found
        return found


def _normalize_existing_lead_link(convo) -> None:
    """If record points to Lead but name exists in CRM Lead, relink to CRM Lead route."""
    try:
        if not safe_ai_exists("DocType", "CRM Lead"):
            return
    except WAChatHubSecurityError:
        return
    lead_name = get_conversation_crm_lead(convo)
    if lead_name:
        set_conversation_crm_lead(convo, lead_name)
        updates = {
            "linked_reference_doctype": convo.linked_reference_doctype,
            "linked_reference_name": convo.linked_reference_name,
        }
        assert_ai_doctype_permission("Chat Conversation", "read")
        if frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
            updates["linked_crm_lead"] = getattr(convo, "linked_crm_lead", None)
        _set_conversation_fields(convo, updates)
        return
    if convo.linked_reference_doctype != "Lead" or not convo.linked_reference_name:
        return
    try:
        crm_lead_exists = safe_ai_exists("CRM Lead", convo.linked_reference_name)
    except WAChatHubSecurityError:
        return
    if crm_lead_exists:
        set_conversation_crm_lead(convo, convo.linked_reference_name)
        updates = {
            "linked_reference_doctype": convo.linked_reference_doctype,
            "linked_reference_name": convo.linked_reference_name,
        }
        assert_ai_doctype_permission("Chat Conversation", "read")
        if frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
            updates["linked_crm_lead"] = getattr(convo, "linked_crm_lead", None)
        _set_conversation_fields(convo, updates)


def _sanitize_contact_links(contact) -> None:
    changed = False

    linked_lead = getattr(contact, "linked_lead", None)
    if linked_lead:
        try:
            lead_exists = safe_ai_exists("DocType", "Lead") and safe_ai_exists("Lead", linked_lead)
        except WAChatHubSecurityError:
            lead_exists = True
        if not lead_exists:
            contact.linked_lead = None
            changed = True

    linked_patient = getattr(contact, "linked_patient", None)
    if linked_patient:
        try:
            patient_exists = not safe_ai_exists("DocType", "Patient") or safe_ai_exists("Patient", linked_patient)
        except WAChatHubSecurityError:
            patient_exists = True
        if not patient_exists:
            contact.linked_patient = None
            changed = True

    if changed:
        _set_contact_fields(
            contact,
            {
                "linked_lead": getattr(contact, "linked_lead", None),
                "linked_patient": getattr(contact, "linked_patient", None),
            },
        )


def _sanitize_conversation_links(convo) -> None:
    crm_lead = getattr(convo, "linked_crm_lead", None)
    if crm_lead:
        try:
            crm_lead_exists = safe_ai_exists("CRM Lead", crm_lead)
        except WAChatHubSecurityError:
            crm_lead_exists = True
        if not crm_lead_exists:
            _set_conversation_fields(
                convo,
                {
                    "linked_crm_lead": None,
                    "linked_reference_doctype": None,
                    "linked_reference_name": None,
                },
            )
            return

    ref_doctype = getattr(convo, "linked_reference_doctype", None)
    ref_name = getattr(convo, "linked_reference_name", None)
    if not ref_doctype or not ref_name:
        return
    try:
        ref_doctype_exists = safe_ai_exists("DocType", ref_doctype)
    except WAChatHubSecurityError:
        return
    if not ref_doctype_exists:
        _set_conversation_fields(
            convo,
            {
                "linked_reference_doctype": None,
                "linked_reference_name": None,
            },
        )
        return
    try:
        ref_exists = safe_ai_exists(ref_doctype, ref_name)
    except WAChatHubSecurityError:
        return
    if not ref_exists:
        _set_conversation_fields(
            convo,
            {
                "linked_reference_doctype": None,
                "linked_reference_name": None,
            },
        )


def _resolve_whatsapp_source_value(meta) -> Optional[str]:
    """Return safe source value for target lead doctype, or None if unavailable."""
    source_df = next((f for f in meta.fields if f.fieldname == "source"), None)
    if not source_df:
        return None

    # For Link fields, ensure the linked master row exists.
    if source_df.fieldtype == "Link" and source_df.options:
        try:
            if safe_ai_exists(source_df.options, "WhatsApp"):
                return "WhatsApp"
        except WAChatHubSecurityError:
            return None
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
        assert_ai_doctype_permission(doctype, "read")
        meta = frappe.get_meta(doctype)
        if safe_ai_exists(doctype, value):
            return value

        title_field = _link_title_field(meta)
        if title_field:
            existing = safe_ai_get_value(doctype, {title_field: value}, "name")
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
        safe_ai_insert(doc)
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


def _persist_inbound_attachment(message_doc, payload: Dict[str, Any]) -> Optional[str]:
    """Persist inbound media as File linked to Chat Message."""
    if str(payload.get("direction") or "").title() != "Inbound":
        return None
    media_url = str(payload.get("media_url") or "").strip()
    if not media_url:
        return None

    filename = build_attachment_filename(payload, media_url)
    existing = safe_ai_get_value(
        "File",
        {
            "attached_to_doctype": "Chat Message",
            "attached_to_name": message_doc.name,
            "file_url": media_url,
        },
        "name",
    )
    if existing:
        assert_ai_doctype_permission("Chat Message", "read")
        if frappe.get_meta("Chat Message").has_field("attachment_file"):
            safe_ai_set_value("Chat Message", message_doc.name, "attachment_file", existing, update_modified=False)
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
    safe_ai_insert(file_doc)
    _repair_remote_attachment_comment_url(message_doc.doctype, message_doc.name, file_doc.file_name, file_doc.file_url)
    assert_ai_doctype_permission("Chat Message", "read")
    if frappe.get_meta("Chat Message").has_field("attachment_file"):
        safe_ai_set_value("Chat Message", message_doc.name, "attachment_file", file_doc.name, update_modified=False)
    return file_doc.name


def _attach_outbound_file_to_message(message_doc, file_name: str) -> None:
    """Move the uploaded local file from conversation-level staging onto the Chat Message."""
    if not file_name or not safe_ai_exists("File", file_name):
        return

    updates = {
        "attached_to_doctype": "Chat Message",
        "attached_to_name": message_doc.name,
    }
    safe_ai_set_value("File", file_name, updates, update_modified=False)
    assert_ai_doctype_permission("Chat Message", "read")
    if frappe.get_meta("Chat Message").has_field("attachment_file"):
        safe_ai_set_value("Chat Message", message_doc.name, "attachment_file", file_name, update_modified=False)


def _sync_inbound_attachment_to_linked_record(
    conversation: str,
    chat_file_name: str,
    message_name: str,
    payload: Dict[str, Any],
) -> None:
    """Mirror inbound chat media URL on linked CRM Lead without local/S3 copy."""
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    crm_lead = get_conversation_crm_lead(convo)
    if not crm_lead:
        return
    ref_doctype = "CRM Lead"
    ref_name = crm_lead

    media_url = str(payload.get("media_url") or "").strip()
    if not media_url:
        return

    chat_file = safe_ai_get_doc("File", chat_file_name)
    filename = build_attachment_filename(payload, media_url)
    lead_filename = f"WA-{message_name}-{filename}"
    existing_lead_file = safe_ai_get_value(
        "File",
        {
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
            "file_name": lead_filename,
        },
        "name",
    )
    if existing_lead_file:
        lead_file_doc = safe_ai_get_doc("File", existing_lead_file)
        _repair_remote_attachment_comment_url(ref_doctype, ref_name, lead_file_doc.file_name, lead_file_doc.file_url)
        return
    existing_lead_file = safe_ai_get_value(
        "File",
        {
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
            "file_url": chat_file.file_url,
        },
        "name",
    )
    if existing_lead_file:
        lead_file_doc = safe_ai_get_doc("File", existing_lead_file)
        _repair_remote_attachment_comment_url(ref_doctype, ref_name, lead_file_doc.file_name, lead_file_doc.file_url)
        return
    lead_file = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": lead_filename,
            "file_url": chat_file.file_url or media_url,
            "is_private": 0,
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
        }
    )
    safe_ai_insert(lead_file)
    _repair_remote_attachment_comment_url(ref_doctype, ref_name, lead_file.file_name, lead_file.file_url)


def _sync_outbound_attachment_to_linked_record(
    conversation: str,
    chat_file_name: str,
    message_name: str,
    payload: Dict[str, Any],
) -> None:
    """Mirror outbound chat media on linked CRM Lead."""
    if not chat_file_name or not safe_ai_exists("File", chat_file_name):
        return

    convo = safe_ai_get_doc("Chat Conversation", conversation)
    crm_lead = get_conversation_crm_lead(convo)
    if not crm_lead:
        return

    ref_doctype = "CRM Lead"
    ref_name = crm_lead
    chat_file = safe_ai_get_doc("File", chat_file_name)
    file_url = str(chat_file.file_url or payload.get("media_url") or "").strip()
    if not file_url:
        return

    lead_filename = f"WA-{message_name}-{chat_file.file_name or payload.get('file_name') or 'attachment'}"
    existing_lead_file = safe_ai_get_value(
        "File",
        {
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
            "file_url": file_url,
        },
        "name",
    )
    if existing_lead_file:
        lead_file_doc = safe_ai_get_doc("File", existing_lead_file)
        _repair_remote_attachment_comment_url(ref_doctype, ref_name, lead_file_doc.file_name, lead_file_doc.file_url)
        return

    lead_file = frappe.get_doc(
        {
            "doctype": "File",
            "file_name": lead_filename,
            "file_url": file_url,
            "is_private": 0,
            "attached_to_doctype": ref_doctype,
            "attached_to_name": ref_name,
        }
    )
    safe_ai_insert(lead_file)
    _repair_remote_attachment_comment_url(ref_doctype, ref_name, lead_file.file_name, lead_file.file_url)


def _is_remote_url(value: str) -> bool:
    try:
        return urlsplit(str(value or "")).scheme in {"http", "https"}
    except Exception:
        return False


def _repair_remote_attachment_comment_url(
    attached_to_doctype: str,
    attached_to_name: str,
    file_name: str,
    file_url: str,
) -> None:
    if not _is_remote_url(file_url):
        return

    comments = safe_ai_get_all(
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
        repaired = _replace_first_href(content, _safe_remote_href(file_url))
        if repaired != content:
            safe_ai_set_value("Comment", comment.name, "content", repaired, update_modified=False)
            return


def _safe_remote_href(url: str) -> str:
    if not _is_remote_url(url):
        return url
    try:
        parts = urlsplit(url)
        query = "&".join(_safe_query_part(part) for part in parts.query.split("&") if part)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment.replace("#", "%23")))
    except Exception:
        return str(url or "").replace("+", "%2B").replace("=", "%3D")


def _safe_query_part(part: str) -> str:
    key, separator, value = part.partition("=")
    if not separator:
        return _safe_query_value(key)
    return f"{_safe_query_value(key)}={_safe_query_value(value)}"


def _safe_query_value(value: str) -> str:
    return quote(unquote(str(value or "").replace("+", "%2B")), safe="")


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
