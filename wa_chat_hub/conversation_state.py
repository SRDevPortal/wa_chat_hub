from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


DECISION_RATE_QUOTE_SENT = "shipping_rate.quote_sent"
DECISION_RATE_PICKUP_REQUESTED = "shipping_rate.pickup_requested"
DECISION_RATE_DELIVERY_REQUESTED = "shipping_rate.delivery_requested"
DECISION_RATE_WEIGHT_REQUESTED = "shipping_rate.weight_requested"
DECISION_OVERWEIGHT_SPLIT_REQUESTED = "shipping_weight.split_requested"
DECISION_OVERWEIGHT_SPLIT_ACCEPTED = "shipping_weight.split_accepted"
DECISION_OVERWEIGHT_SPLIT_DECLINED = "shipping_weight.split_declined"

SUCCESSFUL_DELIVERY_STATUSES = frozenset({"Sent", "Delivered", "Read"})


@dataclass(frozen=True)
class ConversationReply:
    text: str = ""
    decision_code: str = ""
    metadata: dict[str, Any] | None = None


def message_value(row: Any, fieldname: str, default=None):
    if isinstance(row, dict):
        return row.get(fieldname, default)
    return getattr(row, fieldname, default)


def transport_metadata(row: Any) -> dict:
    direct = message_value(row, "reply_metadata")
    if isinstance(direct, dict):
        return dict(direct)

    payload = message_value(row, "raw_transport_payload")
    if isinstance(payload, dict):
        parsed = payload
    else:
        try:
            parsed = json.loads(str(payload or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = {}
    metadata = parsed.get("reply_metadata") if isinstance(parsed, dict) else None
    return dict(metadata) if isinstance(metadata, dict) else {}


def decision_code(row: Any) -> str:
    direct = message_value(row, "decision_code")
    if direct:
        return str(direct).strip()
    return str(transport_metadata(row).get("decision_code") or "").strip()


def is_customer_visible(row: Any) -> bool:
    if str(message_value(row, "direction") or "").strip() != "Outbound":
        return True
    status = str(message_value(row, "delivery_status") or "").strip()
    return not status or status in SUCCESSFUL_DELIVERY_STATUSES


def last_visible_outbound(history) -> Any | None:
    for row in reversed(list(history or [])):
        if str(message_value(row, "direction") or "").strip() != "Outbound":
            continue
        if is_customer_visible(row):
            return row
    return None


def classify_binary_reply(text: str | None) -> bool | None:
    normalized = re.sub(
        r"[^a-z0-9\u0900-\u097F]+",
        " ",
        str(text or "").strip().lower(),
    ).strip()
    if not normalized:
        return None
    if re.fullmatch(
        r"(?:yes|yeah|yep|sure|ok|okay|haan|han|ha|ji|kar sakte hain|kar sakta hoon|kar sakti hoon|split kar denge|split kar dunga|split kar dungi)",
        normalized,
    ):
        return True
    if re.fullmatch(
        r"(?:no|nope|nahi|nahin|nhi|possible nahi|split nahi|cannot split|can t split|cant split|not possible)",
        normalized,
    ):
        return False
    return None
