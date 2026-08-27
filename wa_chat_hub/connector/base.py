from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class NormalizedEvent:
    event_type: str
    channel_account: str
    direction: Optional[str] = None
    sender_type: Optional[str] = None
    phone_number: Optional[str] = None
    display_name: Optional[str] = None
    body: Optional[str] = None
    content_type: str = "Text"
    media_url: Optional[str] = None
    channel_message_id: Optional[str] = None
    delivery_status: Optional[str] = None
    detected_department: Optional[str] = None
    priority: Optional[str] = None
    raw_payload: Dict[str, Any] = field(default_factory=dict)


class ConnectorAdapter:
    connector_type = "base"

    def normalize_inbound(self, payload: Dict[str, Any]) -> NormalizedEvent:
        raise NotImplementedError

    def normalize_status(self, payload: Dict[str, Any]) -> NormalizedEvent:
        raise NotImplementedError

    def build_outbound_payload(self, message: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError
