from __future__ import annotations

import json
import time
from typing import Any, Dict

import frappe
import requests
from frappe import _

from wa_chat_hub.connector.registry import get_adapter
from wa_chat_hub.delivery_outcomes import (
    PatientTemplateDeliveryError,
    PatientTemplateNotSentError,
    PatientTemplateOutcomeUnknownError,
)
from wa_chat_hub.messaging.windows import evaluate_send_permission
from wa_chat_hub.security import safe_ai_get_doc
from wa_chat_hub.task_logger import elapsed, task_log


def build_outbound_message_payload(
    conversation: str,
    body: str | None,
    content_type: str = "Text",
    media_url: str | None = None,
    file_name: str | None = None,
) -> Dict[str, Any]:
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    contact = safe_ai_get_doc("Chat Contact", convo.contact)
    account = safe_ai_get_doc("Chat Channel Account", convo.channel_account)
    adapter = get_adapter(account.channel_type)

    if not (contact.phone_number or "").strip():
        frappe.throw(
            _("Contact phone number is missing on this conversation. Cannot send WhatsApp messages."),
            title=_("Missing Phone Number"),
        )

    normalized = {
        "phone_number": contact.phone_number,
        "body": body,
        "content_type": content_type,
        "media_url": media_url,
        "file_name": file_name,
        "conversation": conversation,
        "channel_account": convo.channel_account,
        "default_country_code": getattr(account, "interakt_default_country_code", None) or "+91",
        "language_code": getattr(account, "interakt_default_language_code", None) or "en",
    }
    return {
        "channel_type": account.channel_type,
        "channel_account": convo.channel_account,
        "payload": adapter.build_outbound_payload(normalized),
    }


def send_outbound_message(
    conversation: str,
    body: str | None,
    content_type: str = "Text",
    media_url: str | None = None,
    file_name: str | None = None,
) -> Dict[str, Any]:
    """Build and send an outbound WhatsApp message through the configured provider."""
    started = time.monotonic()
    task_log(
        "outbound",
        "send_start",
        conversation=conversation,
        content_type=content_type,
        has_media=1 if media_url else 0,
    )
    evaluate_send_permission(conversation, content_type).ensure_allowed(content_type)
    outbound = build_outbound_message_payload(conversation, body, content_type, media_url, file_name=file_name)
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    account = safe_ai_get_doc("Chat Channel Account", convo.channel_account)

    try:
        if account.channel_type == "Interakt":
            result = send_interakt_message(account, outbound)
        else:
            result = send_provider_message(account, outbound)

        task_log(
            "outbound",
            "send_done",
            conversation=conversation,
            channel_type=account.channel_type,
            delivery_status=result.get("delivery_status"),
            duration_sec=elapsed(started),
        )
        return result
    except Exception as exc:
        task_log(
            "outbound",
            "send_failed",
            conversation=conversation,
            channel_type=account.channel_type,
            duration_sec=elapsed(started),
            error=str(exc)[:140],
        )
        raise


def send_provider_message(account, outbound: Dict[str, Any]) -> Dict[str, Any]:
    if not account.provider_base_url:
        return {
            **outbound,
            "sent": False,
            "delivery_status": "Pending",
            "warning": "Provider Base URL is not configured",
        }

    if not account.phone_id or not account.waba_id:
        frappe.throw("Phone ID and WABA ID are required for provider sending", title="Missing WhatsApp Account IDs")

    url = f"{account.provider_base_url.rstrip('/')}/send-message"
    params = {"phoneId": account.phone_id, "wabaId": account.waba_id}

    headers = {
        "Content-Type": "application/json",
        "x_api_key": account.get_password("x_api_key") or "",
        "x_user_id": account.x_user_id or "",
        "x_tenant_id": account.x_tenant_id or "",
    }

    started = time.monotonic()
    task_log("provider", "request_start", channel_type=getattr(account, "channel_type", None))
    response = requests.post(
        url,
        params=params,
        headers=headers,
        json=outbound["payload"],
        timeout=10,
    )
    task_log(
        "provider",
        "request_done",
        channel_type=getattr(account, "channel_type", None),
        status_code=response.status_code,
        duration_sec=elapsed(started),
    )
    if not response.ok:
        frappe.log_error(
            f"WhatsApp provider API Error {response.status_code}: {response.text}\nPayload: {frappe.as_json(outbound['payload'])}",
            "WhatsApp Provider Send Message Failure",
        )
    response.raise_for_status()

    result = response.json() if response.content else {}
    provider_message_id = None
    if isinstance(result, dict):
        result_value = result.get("result")
        provider_message_id = (result_value.get("messageId") if isinstance(result_value, dict) else None)
        provider_message_id = provider_message_id or (result_value.get("id") if isinstance(result_value, dict) else None)
        provider_message_id = provider_message_id or result.get("messageId") or result.get("id")

    return {
        **outbound,
        "sent": True,
        "delivery_status": "Sent",
        "provider_response": result,
        "provider_message_id": provider_message_id,
    }


def build_interakt_template_payload(conversation: str, template: Dict[str, Any]) -> Dict[str, Any]:
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    contact = safe_ai_get_doc("Chat Contact", convo.contact)
    account = safe_ai_get_doc("Chat Channel Account", convo.channel_account)
    adapter = get_adapter(account.channel_type)

    message = {
        "phone_number": contact.phone_number,
        "content_type": "Template",
        "conversation": conversation,
        "channel_account": convo.channel_account,
        "default_country_code": getattr(account, "interakt_default_country_code", None) or "+91",
        "language_code": template.get("language_code") or getattr(account, "interakt_default_language_code", None) or "en",
        "template": {
            "name": template.get("template_name") or template.get("name"),
            "languageCode": template.get("language_code") or getattr(account, "interakt_default_language_code", None) or "en",
            "headerValues": template.get("header_values") or [],
            "bodyValues": template.get("body_values") or [],
            "buttonValues": template.get("button_values") or {},
            "buttonPayload": template.get("button_payload") or {},
            "fileName": template.get("file_name"),
        },
        "callback_data": template.get("callback_data"),
        "campaign_id": template.get("campaign_id"),
    }
    return {
        "channel_type": account.channel_type,
        "channel_account": convo.channel_account,
        "payload": adapter.build_outbound_payload(message),
    }


def send_interakt_template_message(conversation: str, template: Dict[str, Any]) -> Dict[str, Any]:
    try:
        outbound = build_interakt_template_payload(conversation, template)
        account = safe_ai_get_doc("Chat Channel Account", outbound["channel_account"])
    except PatientTemplateDeliveryError:
        raise
    except Exception as exc:
        raise PatientTemplateNotSentError(str(exc), retryable=False) from exc
    return send_interakt_message(account, outbound)


def send_interakt_message(account, outbound: Dict[str, Any]) -> Dict[str, Any]:
    url = account.interakt_base_url or "https://api.interakt.ai/v1/public/message/"
    api_key = _normalize_interakt_api_key(account.get_password("interakt_api_key"))
    if not api_key:
        raise PatientTemplateNotSentError("Interakt API Key is not configured", retryable=False)

    headers = {
        "Authorization": f"Basic {api_key}",
        "Content-Type": "application/json",
    }
    started = time.monotonic()
    task_log("interakt", "request_start", channel_account=account.name)
    try:
        response = requests.post(url, headers=headers, json=outbound["payload"], timeout=20)
    except requests.RequestException as exc:
        raise PatientTemplateOutcomeUnknownError(str(exc)) from exc
    task_log(
        "interakt",
        "request_done",
        channel_account=account.name,
        status_code=response.status_code,
        duration_sec=elapsed(started),
    )
    if not response.ok:
        detail = (response.text or "")[:800]
        frappe.log_error(
            f"Interakt API Error {response.status_code}: {detail}\nPayload: {frappe.as_json(outbound['payload'])}",
            "Interakt Message API Failure",
        )
        raise PatientTemplateNotSentError(
            _extract_interakt_error_message(response.status_code, detail),
            retryable=response.status_code in {408, 429} or response.status_code >= 500,
        )

    try:
        result = response.json() if response.content else {}
    except ValueError as exc:
        raise PatientTemplateOutcomeUnknownError(
            _("Interakt accepted the request but returned an unreadable response.")
        ) from exc
    provider_message_id = None
    if isinstance(result, dict):
        result_value = result.get("result")
        provider_message_id = result.get("id") or (result_value.get("id") if isinstance(result_value, dict) else None)

    return {
        **outbound,
        "sent": bool(result.get("result", True)) if isinstance(result, dict) else True,
        "delivery_status": "Sent" if response.ok else "Failed",
        "provider_response": result,
        "provider_message_id": provider_message_id,
        "raw_provider_response": json.dumps(result),
    }


def _normalize_interakt_api_key(value: str | None) -> str:
    """Return only the Interakt secret used after the ``Basic`` auth scheme.

    Password fields can retain whitespace copied from the Interakt dashboard. Some
    users also paste the complete ``Basic <key>`` header value into the field.
    Normalizing both forms keeps the generated Authorization header valid.
    """
    api_key = str(value or "").strip()
    if api_key.lower().startswith("basic "):
        api_key = api_key[6:].strip()
    return api_key


def _extract_interakt_error_message(status_code: int, detail: str) -> str:
    message = detail
    try:
        payload = json.loads(detail)
        if isinstance(payload, dict):
            message = (
                payload.get("message")
                or payload.get("error")
                or payload.get("detail")
                or payload.get("result")
                or detail
            )
            if isinstance(message, dict):
                message = message.get("message") or message.get("error") or detail
    except Exception:
        pass
    message = str(message or detail or "Unknown error").strip()
    lowered = message.lower()
    if "24" in lowered or "session" in lowered or "template" in lowered:
        return _(
            "{0} WhatsApp only allows free-text replies within 24 hours of the customer's last message. "
            "Use the Template button to send an approved template."
        ).format(message)
    return _("Interakt API returned HTTP {0}: {1}").format(status_code, message)
