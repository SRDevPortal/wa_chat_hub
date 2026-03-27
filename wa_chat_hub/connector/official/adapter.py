from __future__ import annotations

from typing import Any, Dict

from wa_chat_hub.connector.base import ConnectorAdapter, NormalizedEvent


class OfficialWhatsAppAdapter(ConnectorAdapter):
    connector_type = "official_whatsapp"

    def normalize_inbound(self, payload: Dict[str, Any]) -> NormalizedEvent:
        return NormalizedEvent(
            event_type="inbound_message",
            channel_account=payload["channel_account"],
            direction="Inbound",
            sender_type="Customer",
            phone_number=payload.get("from") or payload.get("phone_number"),
            display_name=payload.get("profile_name") or payload.get("display_name"),
            body=(payload.get("text") or {}).get("body") if isinstance(payload.get("text"), dict) else payload.get("body"),
            content_type=(payload.get("type") or "text").title(),
            channel_message_id=payload.get("messageId") or payload.get("channel_message_id"),
            raw_payload=payload,
        )

    def normalize_status(self, payload: Dict[str, Any]) -> NormalizedEvent:
        return NormalizedEvent(
            event_type="status_update",
            channel_account=payload["channel_account"],
            channel_message_id=payload.get("messageId") or payload.get("channel_message_id"),
            delivery_status=payload.get("status"),
            raw_payload=payload,
        )

    def build_outbound_payload(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": message.get("content_type", "text").lower(),
            "to": message.get("phone_number"),
            "data": {"body": message.get("body")},
        }
