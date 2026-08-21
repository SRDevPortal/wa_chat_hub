from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

import frappe

from wa_chat_hub.prompts import get_conversation_crm_lead
from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_get_value,
    safe_ai_set_value,
)


FINAL_WARNING_MESSAGE = (
    "Ji, ye final warning hai. Kripya apni ShipKia shipping requirement, rate query, "
    "pickup/delivery route, shipment issue, callback time ya onboarding detail share karein. Agar aap unrelated chat "
    "continue karenge to main is conversation mein messaging stop kar dunga."
)
STOP_MESSAGE = (
    "Ji, final warning ke baad bhi aap unrelated chat continue kar rahe hain. "
    "Isliye main ab is conversation mein messaging stop kar raha hoon."
)
IRRELEVANT_FINAL_WARNING_THRESHOLD = 10


@dataclass(frozen=True)
class StopDecision:
    action: str
    reason: str = ""


def evaluate_stop_rule(conversation: str, message_id: str) -> StopDecision:
    """Return autopilot action for repeated irrelevant/time-wasting conversations."""
    latest = safe_ai_get_value(
        "Chat Message",
        message_id,
        ["body", "content_type", "media_url"],
        as_dict=True,
    )
    if not latest:
        return StopDecision("allow")

    if is_protected_customer_intent(
        latest.get("body"),
        latest.get("content_type"),
        latest.get("media_url"),
    ):
        if _conversation_stopped(conversation):
            clear_conversation_stopped(conversation)
        return StopDecision("allow", "protected_intent")

    if _conversation_stopped(conversation):
        return StopDecision("skip", "conversation_stopped")

    if _has_final_warning_after_last_protected_intent(conversation):
        return StopDecision("send_stop", "post_final_warning_irrelevant")

    irrelevant_count = _irrelevant_inbound_count_since_last_protected_intent(conversation)
    if irrelevant_count >= IRRELEVANT_FINAL_WARNING_THRESHOLD:
        return StopDecision("send_final_warning", f"irrelevant_count={irrelevant_count}")

    return StopDecision("allow")


def mark_conversation_stopped(conversation: str) -> None:
    _sync_stopped_score_to_linked_lead(conversation)

    assert_ai_doctype_permission("Chat Conversation", "read")
    meta = frappe.get_meta("Chat Conversation")
    updates = {}
    if meta.has_field("conversation_stopped"):
        updates["conversation_stopped"] = 1
    if meta.has_field("lead_score"):
        updates["lead_score"] = 0
    if meta.has_field("lead_temperature"):
        updates["lead_temperature"] = "Cold"
    if updates:
        safe_ai_set_value("Chat Conversation", conversation, updates, update_modified=False)


def clear_conversation_stopped(conversation: str) -> None:
    assert_ai_doctype_permission("Chat Conversation", "read")
    meta = frappe.get_meta("Chat Conversation")
    if meta.has_field("conversation_stopped"):
        safe_ai_set_value(
            "Chat Conversation",
            conversation,
            {"conversation_stopped": 0},
            update_modified=False,
        )


def is_conversation_stopped(conversation: str) -> bool:
    return _conversation_stopped(conversation)


def is_protected_customer_intent(
    body: str | None,
    content_type: str | None = "Text",
    media_url: str | None = None,
) -> bool:
    content = str(content_type or "Text").title()
    if content in {"Image", "Video", "Audio", "Document", "Sticker"} and str(media_url or "").strip():
        return True

    text = _normalize_text(body)
    if not text:
        return False

    protected_patterns = (
        r"\b(rate|rates|price|pricing|charges|starting rate|exact rate|rate card)\b",
        r"\b(shipping|shipment|courier|parcel|order|orders|tracking|ndr|rto|awb|delivery|pickup|pincode|pin code|zone)\b",
        r"\b(cod|prepaid|weight|kg|gram|grams|surface|express|air|reverse pickup|return)\b",
        r"\b(aggregator|shiprocket|delhivery|xpressbees|ecom express|shadowfax|amazon shipping|current aggregator)\b",
        r"\b(business|store|brand|d2c|b2c|marketplace|website|shopify|woocommerce|monthly|volume)\b",
        r"\b(callback|call back|call|schedule|follow up|sales team|support team|urgent)\b",
        r"\b(hindi|english|language|samajh|samajh nahi|translate)\b",
        r"\b(service|services|help|issue|problem|address|location|available)\b",
        r"(रेट|शिपिंग|कूरियर|पिकअप|डिलीवरी|ऑर्डर|कॉल|वजन|पेमेंट|सीओडी|प्रीपेड)",
        r"(rate|shipping|courier|pickup|delivery|order|call|wajan|payment|cod|prepaid|bhejna|bhejna hai)",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in protected_patterns)


def _conversation_stopped(conversation: str) -> bool:
    assert_ai_doctype_permission("Chat Conversation", "read")
    if not frappe.get_meta("Chat Conversation").has_field("conversation_stopped"):
        return False
    return bool(safe_ai_get_value("Chat Conversation", conversation, "conversation_stopped"))


def _irrelevant_inbound_count_since_last_protected_intent(conversation: str) -> int:
    rows = _load_recent_rows(conversation)
    count = 0
    for row in reversed(rows):
        if row.direction != "Inbound":
            continue
        if is_protected_customer_intent(row.body, row.content_type, row.media_url):
            break
        if str(row.body or "").strip() or str(row.media_url or "").strip():
            count += 1
    return count


def _has_final_warning_after_last_protected_intent(conversation: str) -> bool:
    rows = _load_recent_rows(conversation)
    for row in reversed(rows):
        if row.direction == "Inbound" and is_protected_customer_intent(row.body, row.content_type, row.media_url):
            return False
        if row.direction == "Outbound" and str(row.sender_type or "") == "AI":
            if _looks_like_final_warning(row.body):
                return True
    return False


def _load_recent_rows(conversation: str) -> Iterable:
    return safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["direction", "sender_type", "body", "content_type", "media_url", "creation"],
        order_by="creation asc, name asc",
        limit_page_length=80,
    )


def _looks_like_final_warning(body: str | None) -> bool:
    text = _normalize_text(body)
    if not text:
        return False
    if _normalize_text(FINAL_WARNING_MESSAGE) in text:
        return True
    warning_signals = ("final warning", "unrelated chat", "messaging stop", "shipkia shipping requirement")
    return sum(1 for signal in warning_signals if signal in text) >= 2


def _sync_stopped_score_to_linked_lead(conversation: str) -> None:
    try:
        convo = safe_ai_get_doc("Chat Conversation", conversation)
        lead_name = get_conversation_crm_lead(convo)
        if not lead_name or not safe_ai_exists("CRM Lead", lead_name):
            return
        assert_ai_doctype_permission("CRM Lead", "read")
        meta = frappe.get_meta("CRM Lead")
        updates = {}
        if meta.has_field("lead_score"):
            updates["lead_score"] = 0
        if meta.has_field("lead_temperature"):
            updates["lead_temperature"] = "Cold"
        if updates:
            safe_ai_set_value("CRM Lead", lead_name, updates, update_modified=False)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Conversation Stop Lead Sync Failed")


def _normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())
