from __future__ import annotations

import json
import re
from typing import Any, Dict, Tuple

from wa_chat_hub.connector.base import ConnectorAdapter, NormalizedEvent


STATUS_MAP = {
    "message_api_sent": "Sent",
    "message_api_delivered": "Delivered",
    "message_api_read": "Read",
    "message_api_failed": "Failed",
    "message_campaign_sent": "Sent",
    "message_campaign_delivered": "Delivered",
    "message_campaign_read": "Read",
    "message_campaign_failed": "Failed",
    "message_sent": "Sent",
    "message_delivered": "Delivered",
    "message_read": "Read",
    "message_failed": "Failed",
    "sent": "Sent",
    "delivered": "Delivered",
    "read": "Read",
    "failed": "Failed",
    "Sent": "Sent",
    "Delivered": "Delivered",
    "Read": "Read",
    "Failed": "Failed",
}


def extract_interakt_customer_phone(customer: Any, payload: Dict[str, Any] | None = None) -> str:
    """Resolve customer phone from Interakt webhook shapes (channel_phone_number, country+number, etc.)."""
    sources: list[Dict[str, Any]] = []
    if isinstance(customer, dict):
        sources.append(customer)
    if isinstance(payload, dict):
        sources.append(payload)
        data = payload.get("data")
        if isinstance(data, dict):
            sources.append(data)
            nested_customer = data.get("customer")
            if isinstance(nested_customer, dict) and nested_customer is not customer:
                sources.append(nested_customer)

    for source in sources:
        for key in (
            "channel_phone_number",
            "channelPhoneNumber",
            "phone_number",
            "phoneNumber",
            "whatsapp_phone_number",
            "whatsappPhoneNumber",
            "mobile",
            "mobile_no",
            "from",
            "sender",
            "sender_phone_number",
            "senderPhoneNumber",
            "customer_phone_number",
            "customerPhoneNumber",
        ):
            value = source.get(key)
            if value not in (None, ""):
                country = source.get("country_code") or source.get("countryCode")
                if key in {"phone_number", "phoneNumber"} and country and not str(value).strip().startswith("+"):
                    digits = re.sub(r"\D", "", str(value))
                    country_digits = re.sub(r"\D", "", str(country))
                    if country_digits and len(digits) <= 10:
                        return f"+{country_digits}{digits}"
                return str(value).strip()

        country = source.get("country_code") or source.get("countryCode") or "+91"
        local = source.get("phone_number") or source.get("phoneNumber")
        if local not in (None, ""):
            country_str = str(country).strip()
            if not country_str.startswith("+"):
                country_str = f"+{country_str}"
            local_str = str(local).strip()
            if local_str.startswith("+"):
                return local_str
            return f"{country_str}{local_str}"

    return ""


def split_interakt_phone(phone_number: str, default_country_code: str = "+91") -> Tuple[str, str]:
    """Return Interakt's separate countryCode and phoneNumber values."""
    digits = re.sub(r"\D", "", str(phone_number or ""))
    country_code = default_country_code if str(default_country_code or "").startswith("+") else f"+{default_country_code}"

    if not digits:
        return country_code, ""

    country_digits = re.sub(r"\D", "", country_code)
    if country_digits and digits.startswith(country_digits):
        return country_code, digits[len(country_digits):]

    if len(digits) > 10:
        inferred_country = digits[:-10]
        if country_digits == "91" and inferred_country in {"0", "9"}:
            return country_code, digits[-10:]
        return f"+{digits[:-10]}", digits[-10:]

    return country_code, digits


class InteraktAdapter(ConnectorAdapter):
    connector_type = "interakt"

    def normalize_inbound(self, payload: Dict[str, Any]) -> NormalizedEvent:
        data = payload.get("data") or {}
        customer = data.get("customer") or {}
        message = data.get("message") or {}
        traits = customer.get("traits") or {}

        body = (
            message.get("message")
            or message.get("text")
            or message.get("body")
            or message.get("caption")
            or message.get("file_name")
            or message.get("filename")
        )
        if isinstance(body, str):
            body = _extract_message_text(body)
        if _is_empty_interakt_text(body):
            body = None

        content_type = _normalize_content_type(message.get("message_content_type") or message.get("content_type") or message.get("type") or "Text")
        media_url = extract_interakt_media_url(message)
        if media_url and not body:
            body = f"[{content_type} message received]"

        phone_number = extract_interakt_customer_phone(customer, payload)
        if not phone_number and isinstance(message, dict):
            phone_number = extract_interakt_customer_phone(message, payload)

        return NormalizedEvent(
            event_type="inbound_message",
            channel_account=payload["channel_account"],
            direction="Inbound",
            sender_type="Customer",
            phone_number=phone_number,
            display_name=traits.get("name") or customer.get("name") or payload.get("display_name"),
            body=body,
            content_type=content_type,
            media_url=media_url,
            channel_message_id=message.get("id") or payload.get("channel_message_id"),
            raw_payload=payload,
        )

    def normalize_status(self, payload: Dict[str, Any]) -> NormalizedEvent:
        data = payload.get("data") or {}
        message = data.get("message") or {}
        event_type = payload.get("type") or ""
        status = STATUS_MAP.get(event_type) or STATUS_MAP.get(message.get("message_status")) or message.get("message_status")

        return NormalizedEvent(
            event_type="status_update",
            channel_account=payload["channel_account"],
            channel_message_id=message.get("id") or payload.get("channel_message_id"),
            delivery_status=status,
            raw_payload=payload,
        )

    def build_outbound_payload(self, message: Dict[str, Any]) -> Dict[str, Any]:
        country_code, phone_number = split_interakt_phone(
            message.get("phone_number"),
            message.get("default_country_code") or "+91",
        )
        content_type = str(message.get("content_type") or "Text")

        if content_type.lower() == "template" or message.get("template"):
            template = message.get("template") or {}
            payload = {
                "countryCode": country_code,
                "phoneNumber": phone_number,
                "type": "Template",
                "template": {
                    "name": template.get("name") or message.get("template_name"),
                    "languageCode": template.get("languageCode") or message.get("language_code") or "en",
                },
            }
            for source_key, target_key in (
                ("headerValues", "headerValues"),
                ("bodyValues", "bodyValues"),
                ("buttonValues", "buttonValues"),
                ("buttonPayload", "buttonPayload"),
                ("fileName", "fileName"),
            ):
                value = template.get(source_key) or message.get(_camel_to_snake(source_key))
                if value:
                    payload["template"][target_key] = value
            if message.get("callback_data"):
                payload["callbackData"] = message.get("callback_data")
            if message.get("campaign_id"):
                payload["campaignId"] = message.get("campaign_id")
            return payload

        if content_type.lower() in {"image", "video", "audio", "document", "sticker"}:
            data = {"mediaUrl": message.get("media_url")}
            if message.get("body") and content_type.lower() != "sticker":
                data["message"] = message.get("body")
            if message.get("file_name"):
                data["fileName"] = message.get("file_name")
            return {
                "countryCode": country_code,
                "phoneNumber": phone_number,
                "type": content_type.title(),
                "data": data,
            }

        return {
            "countryCode": country_code,
            "phoneNumber": phone_number,
            "type": content_type.title(),
            "data": {
                "message": message.get("body"),
            },
        }


def _extract_message_text(value: str) -> str:
    try:
        parsed = json.loads(value)
    except Exception:
        return value
    if isinstance(parsed, dict):
        return parsed.get("text") or parsed.get("body") or parsed.get("message") or value
    return value


def _is_empty_interakt_text(value: Any) -> bool:
    return str(value or "").strip().lower() in {"", "none", "null", "undefined"}


def _camel_to_snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


def _normalize_content_type(value: Any) -> str:
    normalized = str(value or "Text").strip()
    lowered = normalized.lower()
    aliases = {
        "text": "Text",
        "image": "Image",
        "video": "Video",
        "video_note": "Video",
        "videonote": "Video",
        "audio": "Audio",
        "voice": "Audio",
        "voice_note": "Audio",
        "voicenote": "Audio",
        "ptt": "Audio",
        "document": "Document",
        "file": "Document",
        "sticker": "Sticker",
        "template": "Template",
        "location": "Location",
    }
    return aliases.get(lowered, normalized.title())


def extract_interakt_media_url(message: Dict[str, Any] | str | None) -> str | None:
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except Exception:
            return None
    if not isinstance(message, dict):
        return None

    for key in (
        "media_url",
        "mediaUrl",
        "provider_file_url",
        "providerFileUrl",
        "file_url",
        "fileUrl",
        "download_url",
        "downloadUrl",
        "media",
        "url",
        "link",
    ):
        if message.get(key):
            value = message.get(key)
            if isinstance(value, str):
                return value

    for key in ("data", "message", "file", "attachment", "attachments", "image", "video", "audio", "document", "sticker"):
        value = message.get(key)
        if isinstance(value, list):
            for item in value:
                nested = extract_interakt_media_url(item)
                if nested:
                    return nested
        if isinstance(value, dict):
            nested = extract_interakt_media_url(value)
            if nested:
                return nested

    return None
