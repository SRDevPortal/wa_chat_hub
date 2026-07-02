from __future__ import annotations

import time

import frappe
import requests
from frappe import _
from frappe.utils.file_manager import save_file

from wa_chat_hub.interakt.templates_api import (
    fetch_approved_templates,
    resolve_channel_account_from_conversation,
)
from wa_chat_hub.outbound import send_interakt_template_message, send_outbound_message
from wa_chat_hub.permissions import ensure_can_read_conversation
from wa_chat_hub.services import append_message
from wa_chat_hub.security import safe_ai_get_doc, safe_ai_set_value, set_ai_security_context, set_service_user_context
from wa_chat_hub.task_logger import elapsed, task_log


@frappe.whitelist()
def get_runtime_context(department=None):
    from wa_chat_hub.mcp import build_mcp_runtime_context

    return {"success": True, "result": build_mcp_runtime_context(department=department)}


@frappe.whitelist(methods=["POST"])
def upload_image_for_send():
    """Upload an image to Interakt and return its hosted media URL."""
    return _upload_media_for_send({"image/"}, _("Image file is required"), _("Only image files are supported right now"))


@frappe.whitelist(methods=["POST"])
def upload_document_for_send():
    """Upload a document to Interakt and return its hosted media URL."""
    allowed = {
        "application/pdf",
        "text/plain",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/octet-stream",
    }
    return _upload_media_for_send(allowed, _("Document file is required"), _("Only document files are supported right now"))


def _upload_media_for_send(allowed_mimetypes, missing_file_message, invalid_file_message):
    conversation = frappe.form_dict.get("conversation")
    if not conversation:
        frappe.throw(_("conversation is required"))
    ensure_can_read_conversation(conversation)

    file_storage = frappe.request.files.get("file") if frappe.request and frappe.request.files else None
    if not file_storage:
        frappe.throw(missing_file_message)

    mimetype = file_storage.mimetype or ""
    if not _is_allowed_mimetype(mimetype, allowed_mimetypes):
        frappe.throw(invalid_file_message)

    filename = _safe_public_filename(file_storage.filename or "wa-media")
    content = file_storage.stream.read()
    if not content:
        frappe.throw(_("File is empty"))

    convo = frappe.get_doc("Chat Conversation", conversation)
    account = frappe.get_doc("Chat Channel Account", convo.channel_account)
    if account.channel_type != "Interakt":
        frappe.throw(_("Media upload is currently implemented for Interakt accounts only"))

    api_key = account.get_password("interakt_api_key")
    if not api_key:
        frappe.throw(_("Interakt API Key is not configured"))

    from wa_chat_hub.interakt.account_config import media_upload_api_url

    upload_url = media_upload_api_url(account)
    response = requests.post(
        upload_url,
        params={"fileCategory": "message_template_media"},
        headers={"Authorization": f"Basic {api_key}"},
        files={"uploadFile": (filename, content, mimetype)},
        timeout=30,
    )
    if not response.ok:
        frappe.log_error(
            f"Interakt Media Upload Error {response.status_code}: {response.text}",
            "Interakt Media Upload Failure",
        )
    response.raise_for_status()

    result = response.json() if response.content else {}
    data = result.get("data") if isinstance(result, dict) else {}
    media_url = (data or {}).get("file_url")
    if not media_url:
        frappe.throw(_("Interakt did not return a media URL"))

    local_file = save_file(
        filename,
        content,
        "Chat Conversation",
        conversation,
        decode=False,
        is_private=0,
    )

    return {
        "success": True,
        "result": {
            "file": local_file.name,
            "file_url": local_file.file_url,
            "media_url": media_url,
            "provider_file_url": media_url,
            "mimetype": mimetype,
            "file_name": local_file.file_name or (data or {}).get("file_name") or filename,
            "file_size": _format_file_size(len(content)),
            "file_size_bytes": len(content),
            "file_handle": (data or {}).get("file_handle"),
            "provider_response": result,
        },
    }


@frappe.whitelist(methods=["POST"])
def send_reply():
    started = time.monotonic()
    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()

    conversation = payload.get("conversation")
    body = payload.get("body")
    media_url = payload.get("media_url")
    attachment_file = payload.get("attachment_file") or payload.get("file")
    file_name = payload.get("file_name")
    file_size = payload.get("file_size")
    display_media_url = payload.get("display_media_url") or media_url
    if not conversation:
        frappe.throw(_("conversation is required"))
    ensure_can_read_conversation(conversation)

    content_type = str(payload.get("content_type") or "Text").title()
    is_media_message = content_type in {"Image", "Document", "Audio", "Video", "Sticker"}
    if is_media_message and not media_url:
        frappe.throw(_("media_url is required for media messages"))
    if not is_media_message and not body:
        frappe.throw(_("body is required"))

    task_log(
        "runtime",
        "send_reply_start",
        conversation=conversation,
        content_type=content_type,
        has_media=1 if media_url else 0,
    )
    from wa_chat_hub.messaging.windows import evaluate_send_permission

    evaluate_send_permission(conversation, content_type).ensure_allowed(content_type)

    convo = frappe.get_doc("Chat Conversation", conversation)
    result = append_message({
        "channel_account": convo.channel_account,
        "phone_number": frappe.db.get_value("Chat Contact", convo.contact, "phone_number"),
        "direction": "Outbound",
        "sender_type": payload.get("sender_type", "Agent"),
        "content_type": content_type,
        "body": body,
        "media_url": display_media_url,
        "delivery_status": "Pending",
        "attachment_file": attachment_file,
        "raw_transport_payload": {
            "queued": True,
            "file_name": file_name,
            "file_size": file_size,
            "attachment_file": attachment_file,
            "provider_media_url": media_url,
        },
    })
    _enqueue_pending_reply_send(
        message_name=result.get("message"),
        conversation=conversation,
        body=body,
        content_type=content_type,
        media_url=media_url,
        file_name=file_name,
    )
    task_log(
        "runtime",
        "send_reply_queued",
        conversation=conversation,
        content_type=content_type,
        message=result.get("message"),
        duration_sec=elapsed(started),
    )
    return {
        "success": True,
        "result": {
            **result,
            "queued": True,
            "sent": False,
            "delivery_status": "Pending",
        },
    }


def _enqueue_pending_reply_send(
    *,
    message_name: str | None,
    conversation: str,
    body: str | None,
    content_type: str,
    media_url: str | None,
    file_name: str | None,
) -> None:
    if not message_name:
        frappe.throw(_("Could not queue outbound message: local message was not created"))
    job_id = f"wa_send_reply_{message_name}"
    frappe.enqueue(
        "wa_chat_hub.api.runtime.send_pending_reply_to_provider",
        queue="short",
        timeout=60,
        enqueue_after_commit=True,
        now=frappe.flags.in_test,
        job_id=job_id,
        deduplicate=True,
        message_name=message_name,
        conversation=conversation,
        body=body,
        content_type=content_type,
        media_url=media_url,
        file_name=file_name,
    )
    task_log(
        "runtime",
        "send_reply_enqueue",
        conversation=conversation,
        message=message_name,
        queue="short",
    )


def send_pending_reply_to_provider(
    message_name: str,
    conversation: str,
    body: str | None = None,
    content_type: str = "Text",
    media_url: str | None = None,
    file_name: str | None = None,
) -> dict:
    started = time.monotonic()
    set_service_user_context("manual_reply_send")
    set_ai_security_context(operation="manual_reply_send", conversation=conversation, message=message_name)
    task_log(
        "runtime",
        "send_reply_provider_start",
        conversation=conversation,
        message=message_name,
        content_type=content_type,
    )
    try:
        message = safe_ai_get_doc("Chat Message", message_name)
        if getattr(message, "delivery_status", None) in {"Sent", "Delivered", "Read"}:
            return {"success": True, "message": message_name, "skipped": "already_sent"}

        outbound = send_outbound_message(conversation, body, content_type, media_url, file_name=file_name)
        updates = {
            "delivery_status": outbound.get("delivery_status") or "Sent",
            "raw_transport_payload": frappe.as_json(
                {
                    **outbound,
                    "file_name": file_name,
                    "provider_media_url": media_url,
                }
            ),
        }
        provider_message_id = outbound.get("provider_message_id")
        meta = frappe.get_meta("Chat Message")
        if provider_message_id:
            if meta.has_field("channel_message_id"):
                updates["channel_message_id"] = provider_message_id
            if meta.has_field("provider_message_id"):
                updates["provider_message_id"] = provider_message_id
        safe_ai_set_value("Chat Message", message_name, updates, update_modified=True)
        task_log(
            "runtime",
            "send_reply_provider_done",
            conversation=conversation,
            message=message_name,
            delivery_status=updates["delivery_status"],
            duration_sec=elapsed(started),
        )
        frappe.publish_realtime(
            "wa_chat_message_status_updated",
            {
                "conversation": conversation,
                "message": message_name,
                "delivery_status": updates["delivery_status"],
            },
            after_commit=True,
        )
        return {"success": True, "message": message_name, "result": outbound}
    except Exception as exc:
        error_text = str(exc)
        try:
            safe_ai_set_value(
                "Chat Message",
                message_name,
                {
                    "delivery_status": "Failed",
                    "raw_transport_payload": frappe.as_json({"error": error_text, "provider_media_url": media_url}),
                },
                update_modified=True,
            )
            frappe.publish_realtime(
                "wa_chat_message_status_updated",
                {"conversation": conversation, "message": message_name, "delivery_status": "Failed"},
                after_commit=True,
            )
        except Exception:
            pass
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Queued Sending Failed")
        task_log(
            "runtime",
            "send_reply_provider_failed",
            conversation=conversation,
            message=message_name,
            duration_sec=elapsed(started),
            error=error_text[:140],
        )
        return {"success": False, "message": message_name, "error": error_text}


@frappe.whitelist()
def get_interakt_templates(conversation=None, channel_account=None, force_refresh=0):
    """Return approved Interakt templates for the conversation's channel account."""
    if conversation and not channel_account:
        ensure_can_read_conversation(conversation)
        channel_account = resolve_channel_account_from_conversation(conversation)
    if not channel_account:
        frappe.throw(_("conversation or channel_account is required"))

    templates = fetch_approved_templates(
        channel_account,
        force_refresh=bool(int(force_refresh or 0)),
    )
    return {
        "success": True,
        "result": {
            "channel_account": channel_account,
            "templates": templates,
            "count": len(templates),
        },
    }


@frappe.whitelist(methods=["POST"])
def send_template_message():
    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()

    conversation = payload.get("conversation")
    template_name = payload.get("template_name") or payload.get("name")
    if not conversation:
        frappe.throw(_("conversation is required"))
    if not template_name:
        frappe.throw(_("template_name is required"))
    ensure_can_read_conversation(conversation)

    template = {
        "template_name": template_name,
        "language_code": payload.get("language_code"),
        "header_values": _list_or_json(payload.get("header_values")),
        "body_values": _list_or_json(payload.get("body_values")),
        "button_values": _dict_or_json(payload.get("button_values")),
        "button_payload": _dict_or_json(payload.get("button_payload")),
        "file_name": payload.get("file_name"),
        "callback_data": payload.get("callback_data"),
        "campaign_id": payload.get("campaign_id"),
        "template_category": payload.get("template_category"),
    }

    try:
        outbound = send_interakt_template_message(conversation, template)
        delivery_status = outbound.get("delivery_status") or "Sent"
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "Interakt Template Sending Failed")
        outbound = {
            "conversation": conversation,
            "sent": False,
            "delivery_status": "Failed",
            "template_name": template_name,
            "error": str(exc),
        }
        delivery_status = "Failed"

    convo = frappe.get_doc("Chat Conversation", conversation)
    body_preview = payload.get("body") or f"Template: {template_name}"
    result = append_message({
        "channel_account": convo.channel_account,
        "phone_number": frappe.db.get_value("Chat Contact", convo.contact, "phone_number"),
        "direction": "Outbound",
        "sender_type": payload.get("sender_type", "Agent"),
        "content_type": "Template",
        "body": body_preview,
        "delivery_status": delivery_status,
        "channel_message_id": outbound.get("provider_message_id"),
        "raw_transport_payload": outbound,
        "template_category": template.get("template_category"),
    })
    return {"success": True, "result": {**outbound, **result}}


@frappe.whitelist(methods=["POST"])
def call_mcp_tool():
    from wa_chat_hub.mcp import invoke_mcp_tool

    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()
    return invoke_mcp_tool(
        server_name=payload.get("server_name"),
        tool_name=payload.get("tool_name"),
        payload=payload.get("payload") or {},
    )


def _list_or_json(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = frappe.parse_json(value)
            return parsed if isinstance(parsed, list) else [value]
        except Exception:
            return [value]
    return [value]


def _dict_or_json(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = frappe.parse_json(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _safe_public_filename(filename: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in {".", "-", "_"} else "-"
        for ch in str(filename or "wa-image")
    ).strip(".-_")
    return cleaned or "wa-media"


def _is_allowed_mimetype(mimetype: str, allowed) -> bool:
    for item in allowed:
        if item.endswith("/") and mimetype.startswith(item):
            return True
        if mimetype == item:
            return True
    return False


def _format_file_size(size: int) -> str:
    value = float(size or 0)
    units = ["B", "kB", "MB", "GB"]
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}".replace(".0 ", " ")
