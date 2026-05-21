from __future__ import annotations

import frappe
import requests
from frappe import _

from wa_chat_hub.interakt.templates_api import (
    fetch_approved_templates,
    resolve_channel_account_from_conversation,
)
from wa_chat_hub.outbound import send_interakt_template_message, send_outbound_message
from wa_chat_hub.services import append_message


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

    upload_url = "https://api.interakt.ai/v1/public/track/files/upload_to_fb/"
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

    return {
        "success": True,
        "result": {
            "file": None,
            "file_url": media_url,
            "media_url": media_url,
            "mimetype": mimetype,
            "file_name": (data or {}).get("file_name") or filename,
            "file_size": _format_file_size(len(content)),
            "file_size_bytes": len(content),
            "file_handle": (data or {}).get("file_handle"),
            "provider_response": result,
        },
    }


@frappe.whitelist(methods=["POST"])
def send_reply():
    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()

    conversation = payload.get("conversation")
    body = payload.get("body")
    media_url = payload.get("media_url")
    file_name = payload.get("file_name")
    file_size = payload.get("file_size")
    display_media_url = payload.get("display_media_url") or media_url
    if not conversation:
        frappe.throw(_("conversation is required"))

    content_type = str(payload.get("content_type") or "Text").title()
    is_media_message = content_type in {"Image", "Document", "Audio", "Video", "Sticker"}
    if is_media_message and not media_url:
        frappe.throw(_("media_url is required for media messages"))
    if not is_media_message and not body:
        frappe.throw(_("body is required"))

    try:
        outbound = send_outbound_message(conversation, body, content_type, media_url, file_name=file_name)
        delivery_status = outbound.get("delivery_status") or "Sent"
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Sending Failed")
        outbound = {
            "conversation": conversation,
            "body": body,
            "content_type": content_type,
            "media_url": media_url,
            "sent": False,
            "delivery_status": "Failed",
            "error": str(exc),
        }
        delivery_status = "Failed"

    convo = frappe.get_doc("Chat Conversation", conversation)
    result = append_message({
        "channel_account": convo.channel_account,
        "phone_number": frappe.db.get_value("Chat Contact", convo.contact, "phone_number"),
        "direction": "Outbound",
        "sender_type": payload.get("sender_type", "Agent"),
        "content_type": content_type,
        "body": body,
        "media_url": display_media_url,
        "delivery_status": delivery_status,
        "channel_message_id": outbound.get("provider_message_id"),
        "raw_transport_payload": {**outbound, "file_name": file_name, "file_size": file_size},
    })
    return {"success": True, "result": {**outbound, **result}}


@frappe.whitelist()
def get_interakt_templates(conversation=None, channel_account=None, force_refresh=0):
    """Return approved Interakt templates for the conversation's channel account."""
    if conversation and not channel_account:
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
