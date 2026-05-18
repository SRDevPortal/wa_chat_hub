from __future__ import annotations

import json
from typing import Any, Dict

import frappe
import requests

from wa_chat_hub.connector.registry import get_adapter


def build_outbound_message_payload(
    conversation: str,
    body: str | None,
    content_type: str = "Text",
    media_url: str | None = None,
    file_name: str | None = None,
) -> Dict[str, Any]:
    convo = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", convo.contact)
    account = frappe.get_doc("Chat Channel Account", convo.channel_account)
    adapter = get_adapter(account.channel_type)

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
    outbound = build_outbound_message_payload(conversation, body, content_type, media_url, file_name=file_name)
    convo = frappe.get_doc("Chat Conversation", conversation)
    account = frappe.get_doc("Chat Channel Account", convo.channel_account)

    if account.channel_type == "Interakt":
        return send_interakt_message(account, outbound)

    return send_provider_message(account, outbound)


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

    response = requests.post(
        url,
        params=params,
        headers=headers,
        json=outbound["payload"],
        timeout=10,
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
    convo = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", convo.contact)
    account = frappe.get_doc("Chat Channel Account", convo.channel_account)
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
    outbound = build_interakt_template_payload(conversation, template)
    account = frappe.get_doc("Chat Channel Account", outbound["channel_account"])
    return send_interakt_message(account, outbound)


def send_interakt_message(account, outbound: Dict[str, Any]) -> Dict[str, Any]:
    url = account.interakt_base_url or "https://api.interakt.ai/v1/public/message/"
    api_key = account.get_password("interakt_api_key")
    if not api_key:
        frappe.throw("Interakt API Key is not configured", title="Missing Interakt API Key")

    headers = {
        "Authorization": f"Basic {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, json=outbound["payload"], timeout=20)
    if not response.ok:
        frappe.log_error(
            f"Interakt API Error {response.status_code}: {response.text}\nPayload: {frappe.as_json(outbound['payload'])}",
            "Interakt Message API Failure",
        )
    response.raise_for_status()

    result = response.json() if response.content else {}
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
