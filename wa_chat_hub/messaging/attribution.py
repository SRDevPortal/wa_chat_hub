"""Extract CTWA / ad attribution from Interakt webhook payloads (shared, no api.chat import)."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import frappe

from wa_chat_hub.security import safe_ai_get_all


def get_conversation_attribution(conversation: str) -> Dict[str, Optional[str]]:
    rows = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation, "direction": "Inbound"},
        fields=["raw_payload", "creation"],
        order_by="creation desc",
        limit_page_length=20,
    )
    for row in rows:
        payload = json_loads_payload(row.raw_payload)
        if not payload:
            continue
        data = extract_attribution(payload)
        if any(data.values()):
            return data
    return {}


def extract_attribution(payload: Dict[str, Any]) -> Dict[str, Optional[str]]:
    referral = find_first_dict(payload, {"referral", "source", "context", "button", "click_to_whatsapp"})
    traits = _interakt_customer_traits(payload)

    source_id = (
        find_first_value(payload, ["source_id", "sourceId", "sourceID", "source_url_id", "Source ID"], referral)
        or find_direct_value(referral, ["id"])
        or find_direct_value(traits, ["W Source_id", "W Source ID", "source_id", "Source ID"])
    )
    source = (
        find_first_value(payload, ["_internal_lead_source", "internal_lead_source", "channel_type"], referral)
        or find_first_value(payload, ["source", "source_type", "sourceType", "Source"], referral)
        or find_direct_value(referral, ["type"])
    )
    return {
        "source_id": source_id,
        "source_url": (
            find_first_value(payload, ["source_url", "sourceUrl", "url", "sourceURL", "Source URL"], referral)
            or find_direct_value(traits, ["W Source_url", "W Source URL", "source_url", "Source URL"])
        ),
        "source": source,
        "ctwa_clid": (
            find_first_value(
                payload, ["ctwa_clid", "ctwaClid", "ctwa_click_id", "click_id", "ctwa clid"], referral
            )
            or find_direct_value(traits, ["W Ctwa_clid", "W CTWA clid", "ctwa_clid", "Ctwa_clid"])
        ),
    }


def _interakt_customer_traits(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Interakt message_received nests ad traits under data.customer.traits."""
    data = payload.get("data")
    if not isinstance(data, dict):
        return {}
    customer = data.get("customer")
    if not isinstance(customer, dict):
        return {}
    traits = customer.get("traits")
    return traits if isinstance(traits, dict) else {}


def json_loads_payload(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def find_first_dict(value: Any, preferred_keys: set) -> Dict[str, Any]:
    if isinstance(value, dict):
        for key, nested in value.items():
            if key in preferred_keys and isinstance(nested, dict):
                return nested
        for nested in value.values():
            found = find_first_dict(nested, preferred_keys)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = find_first_dict(nested, preferred_keys)
            if found:
                return found
    return {}


def find_first_value(payload: Dict[str, Any], keys: list, preferred: Optional[Dict[str, Any]] = None) -> Optional[str]:
    for source in (preferred or {}, payload):
        value = find_value_recursive(source, set(keys))
        if value not in (None, ""):
            return str(value)
    return None


def find_direct_value(value: Dict[str, Any], keys: list) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    normalized = {normalize_key(key) for key in keys}
    for key, found in value.items():
        if normalize_key(key) in normalized and is_scalar(found):
            return str(found)
    for key in keys:
        found = value.get(key)
        if is_scalar(found):
            return str(found)
    return None


def find_value_recursive(value: Any, keys: set) -> Any:
    if isinstance(value, dict):
        for key, nested in value.items():
            if normalize_key(key) in keys and is_scalar(nested):
                return nested
        for nested in value.values():
            found = find_value_recursive(nested, keys)
            if found not in (None, ""):
                return found
    if isinstance(value, list):
        for nested in value:
            found = find_value_recursive(nested, keys)
            if found not in (None, ""):
                return found
    return None


def is_scalar(value: Any) -> bool:
    return value not in (None, "") and not isinstance(value, (dict, list, tuple, set))


def normalize_key(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())
