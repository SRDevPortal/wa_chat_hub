from __future__ import annotations

from typing import Any, Dict

from wa_chat_hub.connector.base import ConnectorAdapter, NormalizedEvent


class PersonalWhatsAppAdapter(ConnectorAdapter):
    connector_type = "personal_whatsapp"

    def normalize_inbound(self, payload: Dict[str, Any]) -> NormalizedEvent:
        return NormalizedEvent(
            event_type="inbound_message",
            channel_account=payload["channel_account"],
            direction="Inbound",
            sender_type="Customer",
            phone_number=payload.get("from") or payload.get("phone_number"),
            display_name=payload.get("pushName") or payload.get("display_name"),
            body=payload.get("body") or payload.get("text"),
            content_type=(payload.get("type") or "text").title(),
            channel_message_id=payload.get("id") or payload.get("channel_message_id"),
            raw_payload=payload,
        )

    def normalize_status(self, payload: Dict[str, Any]) -> NormalizedEvent:
        return NormalizedEvent(
            event_type="session_status_change",
            channel_account=payload["channel_account"],
            delivery_status=payload.get("status"),
            raw_payload=payload,
        )

    def build_outbound_payload(self, message: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "to": message.get("phone_number"),
            "body": message.get("body"),
            "type": message.get("content_type", "Text").lower(),
        }
