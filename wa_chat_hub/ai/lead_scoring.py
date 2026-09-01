from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Dict, List, Tuple

import frappe
from frappe.utils import now_datetime

from wa_chat_hub.ai.language import resolve_language_from_history
from wa_chat_hub.db_retry import with_db_lock_retry
from wa_chat_hub.prompts import get_conversation_crm_lead
from wa_chat_hub.policy import get_conversation_policy
from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_set_value,
)

CITY_ALIASES = {
    "banglore": "Bangalore",
    "bengaluru": "Bangalore",
    "kolkta": "Kolkata",
    "calcutta": "Kolkata",
}

ROUTE_CONNECTOR_PATTERN = r"(?:from\s+)?([A-Za-z][A-Za-z .'-]{1,40}?)\s+(?:to|se)\s+([A-Za-z][A-Za-z .'-]{1,40})"
HINGLISH_TO_FILLERS = {"me", "main", "mai", "mein", "m", "hum", "ham", "we"}
NON_CITY_ROUTE_WORDS = {
    "aggregator",
    "courier",
    "current",
    "currently",
    "hu",
    "hoon",
    "hai",
    "use",
    "using",
    "krrha",
    "krra",
    "kar",
    "karta",
    "karte",
    "shipmoro",
    "shipmozo",
    "shipro",
    "shiprocket",
    "nimbuspost",
    "provider",
}

BUSINESS_TYPE_KEYWORDS = (
    (r"\bd2c\b|\bdirect\s+to\s+consumer\b", "D2C"),
    (r"\bb2c\b|\bbusiness\s+to\s+consumer\b", "B2C"),
    (r"\bb2b\b|\bbusiness\s+to\s+business\b", "B2B"),
    (r"\bwholesale(?:r)?\b", "Wholesale"),
    (r"\bretail(?:er)?\b|\boffline\s+store\b", "Retail"),
    (r"\bmanufactur(?:er|ing)\b|\bfactory\b", "Manufacturing"),
    (r"\breseller\b", "Reseller"),
    (r"\bdistributor\b|\bdistribution\b", "Distributor"),
    (r"\bmarketplace\b|\bamazon\b|\bflipkart\b|\bmeesho\b", "Marketplace Seller"),
    (r"\bsocial\s+commerce\b|\binstagram\b|\bwhatsapp\s+seller\b", "Social Commerce"),
    (r"\bexport(?:er)?\b|\bimport(?:er)?\b", "Export/Import"),
    (r"\btrading\b|\btrader\b", "Trading"),
    (r"\bservice\s+business\b|\bservices\b", "Services"),
    (r"\bother\b|\bothers\b|\bnot\s+listed\b|\balag\b", "Other"),
)


@dataclass
class ScoreResult:
    lead_score: float
    lead_temperature: str
    lead_lan: str
    source: str


@dataclass
class ShipKiaLeadDetails:
    business_type: str | None = None
    business_name: str | None = None
    monthly_shipments: int | None = None
    aggregator_status: str | None = None
    aggregator_name: str | None = None
    aggregator_raw: str | None = None
    current_shipping_rate: float | None = None
    current_rate_zone: str | None = None
    rto_percentage: float | None = None
    pickup_city: str | None = None
    delivery_city: str | None = None
    average_weight: str | None = None
    rate_shared: bool = False
    signup_requested: bool = False
    latest_inbound_at: object | None = None
    inbound_count: int = 0
    asked_for_rates: bool = False

    @property
    def collected_count(self) -> int:
        return len(
            [
                value
                for value in (
                    self.business_type,
                    self.business_name,
                    self.monthly_shipments,
                    self.aggregator_status,
                    self.current_shipping_rate,
                    self.rto_percentage,
                    self.pickup_city,
                    self.delivery_city,
                    self.average_weight,
                )
                if value not in (None, "")
            ]
        )


def score_and_sync_conversation(conversation: str) -> Dict[str, str]:
    result = recompute_conversation_metrics(conversation)
    sync_to_linked_lead(conversation, result)
    sync_to_conversation(conversation, result)
    return {
        "lead_score": f"{result.lead_score:.2f}",
        "lead_temperature": result.lead_temperature,
        "lead_lan": result.lead_lan,
        "source": result.source,
    }


def recompute_conversation_metrics(conversation: str) -> ScoreResult:
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    if getattr(convo, "conversation_stopped", 0):
        return ScoreResult(
            lead_score=0,
            lead_temperature="Cold",
            lead_lan=convo.lead_lan or "English",
            source="conversation_stopped",
        )

    history = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["direction", "body", "creation"],
        order_by="creation asc",
        limit_page_length=40,
    )
    score_result = _policy_score(convo, history)
    return score_result


def sync_to_conversation(conversation: str, result: ScoreResult) -> None:
    """Persist scoring only when at least one derived value changed."""
    assert_ai_doctype_permission("Chat Conversation", "write")
    with_db_lock_retry(
        "conversation_lead_score_update",
        lambda: frappe.db.sql(
            """
            UPDATE `tabChat Conversation`
            SET lead_score = %s,
                lead_temperature = %s,
                lead_lan = %s
            WHERE name = %s
              AND (
                  NOT (lead_score <=> %s)
                  OR NOT (lead_temperature <=> %s)
                  OR NOT (lead_lan <=> %s)
              )
            """,
            (
                result.lead_score,
                result.lead_temperature,
                result.lead_lan,
                conversation,
                result.lead_score,
                result.lead_temperature,
                result.lead_lan,
            ),
        ),
    )


def sync_to_linked_lead(conversation: str, result: ScoreResult | None = None) -> None:
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    linked_lead = _get_conversation_linked_lead(convo)
    if not linked_lead:
        return

    if result is None:
        result = ScoreResult(
            lead_score=float(convo.lead_score or 0),
            lead_temperature=convo.lead_temperature or "Cold",
            lead_lan=convo.lead_lan or "English",
            source="existing",
        )

    target_dt, lead_name = linked_lead
    if not safe_ai_exists(target_dt, lead_name):
        return

    assert_ai_doctype_permission(target_dt, "write")
    meta = frappe.get_meta(target_dt)
    history = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["direction", "body", "creation"],
        order_by="creation asc",
        limit_page_length=100,
    )
    details = _extract_shipkia_lead_details(history)
    updates = _build_lead_updates(meta, result, details)
    if not updates:
        return

    safe_ai_set_value(
        target_dt,
        lead_name,
        updates,
        update_modified=False,
    )


def _get_conversation_linked_lead(convo) -> Tuple[str, str] | None:
    linked_doctype = str(getattr(convo, "linked_reference_doctype", "") or "").strip()
    linked_name = str(getattr(convo, "linked_reference_name", "") or "").strip()
    if linked_doctype in {"Lead", "CRM Lead"} and linked_name:
        return linked_doctype, linked_name

    crm_lead = get_conversation_crm_lead(convo)
    if crm_lead:
        return "CRM Lead", crm_lead

    contact_name = str(getattr(convo, "contact", "") or "").strip()
    if contact_name and safe_ai_exists("Chat Contact", contact_name):
        contact = safe_ai_get_doc("Chat Contact", contact_name)
        source_doctype = str(getattr(contact, "source_doctype", "") or "").strip()
        source_name = str(getattr(contact, "source_name", "") or "").strip()
        if source_doctype in {"Lead", "CRM Lead"} and source_name:
            return source_doctype, source_name
        linked_lead = str(getattr(contact, "linked_lead", "") or "").strip()
        if linked_lead:
            return "Lead", linked_lead

    return None


def _build_lead_updates(meta, result: ScoreResult, details: ShipKiaLeadDetails) -> Dict[str, object]:
    updates: Dict[str, object] = {}
    _add_lead_update(meta, updates, "lead_score", result.lead_score)
    _add_lead_update(meta, updates, "lead_lan", result.lead_lan)
    _add_lead_update(meta, updates, "lead_temperature", result.lead_temperature)
    _add_lead_update(meta, updates, "shipkia_lead_temperature", result.lead_temperature)
    _add_lead_update(meta, updates, "shipkia_ai_lead_temperature", result.lead_temperature)

    _add_lead_update(meta, updates, "shipkia_business_type", details.business_type)
    if details.business_name:
        _add_lead_update(meta, updates, "shipkia_business_name", details.business_name)
        _add_lead_update(meta, updates, "company_name", details.business_name)
    _add_lead_update(meta, updates, "shipkia_monthly_shipments", details.monthly_shipments)
    _add_lead_update(meta, updates, "shipkia_current_aggregator_status", details.aggregator_status)
    _add_lead_update(meta, updates, "shipkia_current_aggregator_name", details.aggregator_name)
    _add_lead_update(meta, updates, "shipkia_current_aggregator_raw", details.aggregator_raw)
    if details.aggregator_status:
        _add_lead_update(meta, updates, "shipkia_current_aggregator_verified", 1)
    _add_lead_update(meta, updates, "shipkia_current_shipping_rate", details.current_shipping_rate)
    _add_lead_update(meta, updates, "shipkia_rto_percentage", details.rto_percentage)
    _add_lead_update(meta, updates, "shipkia_pickup_city", details.pickup_city)
    _add_lead_update(meta, updates, "shipkia_delivery_city", details.delivery_city)
    _add_lead_update(meta, updates, "shipkia_average_weight", details.average_weight)
    if details.rate_shared and meta.has_field("shipkia_rate_shared"):
        updates["shipkia_rate_shared"] = 1

    _add_lead_update(meta, updates, "shipkia_ai_qualification_score", result.lead_score)
    _add_lead_update(meta, updates, "shipkia_ai_messages_count", details.inbound_count)
    _add_lead_update(meta, updates, "shipkia_ai_details_collected", details.collected_count)
    _add_lead_update(meta, updates, "shipkia_ai_last_message_at", details.latest_inbound_at)
    _add_lead_update(meta, updates, "shipkia_context_updated_at", now_datetime())
    _add_lead_update(meta, updates, "shipkia_context_completed", 1 if details.collected_count >= 5 else 0)
    _add_lead_update(meta, updates, "shipkia_ai_context_complete", 1 if details.collected_count >= 5 else 0)
    _add_lead_update(meta, updates, "shipkia_requirement_details", _build_requirement_summary(details))

    qualification_status = _qualification_status(details)
    _add_lead_update(meta, updates, "shipkia_qualification_status", qualification_status)
    _add_lead_update(meta, updates, "shipkia_ai_qualification_status", qualification_status)
    onboarding_stage = "Details Collected" if details.collected_count >= 5 else "In Progress"
    _add_lead_update(
        meta,
        updates,
        "shipkia_ai_onboarding_stage",
        _coerce_select_value(meta, "shipkia_ai_onboarding_stage", onboarding_stage, fallback="Not Started"),
    )
    if details.signup_requested and meta.has_field("shipkia_ai_onboarding_assisted"):
        updates["shipkia_ai_onboarding_assisted"] = 1
    if meta.has_field("shipkia_sales_stage"):
        if details.signup_requested:
            sales_stage = "Demo / Signup Pending"
        elif details.rate_shared:
            sales_stage = "Rate Shared"
        else:
            sales_stage = "Qualified" if qualification_status == "Qualified" else "New Lead"
        _add_lead_update(meta, updates, "shipkia_sales_stage", sales_stage)
    _add_lead_update(meta, updates, "shipkia_lead_source", "WhatsApp Inbound")
    _add_lead_update(meta, updates, "shipkia_first_contact_channel", "WhatsApp")

    return updates


def _add_lead_update(meta, updates: Dict[str, object], fieldname: str, value: object) -> None:
    if value in (None, "") or not meta.has_field(fieldname):
        return
    if not _select_allows_value(meta, fieldname, value):
        return
    updates[fieldname] = value


def _select_allows_value(meta, fieldname: str, value: object) -> bool:
    field = meta.get_field(fieldname)
    if not field or field.fieldtype != "Select":
        return True
    options = _select_options(field)
    return not options or str(value).strip() in options


def _coerce_select_value(meta, fieldname: str, value: str, fallback: str | None = None) -> str | None:
    if _select_allows_value(meta, fieldname, value):
        return value
    if fallback and _select_allows_value(meta, fieldname, fallback):
        return fallback
    return None


def _select_options(field) -> set[str]:
    return {
        option.strip()
        for option in str(getattr(field, "options", "") or "").splitlines()
        if option.strip()
    }


def _extract_shipkia_lead_details(history: List[Dict]) -> ShipKiaLeadDetails:
    details = ShipKiaLeadDetails()
    inbound = []
    full_text_parts = []
    last_outbound = ""

    for row in history:
        body = str(row.get("body") or "").strip()
        if not body:
            continue
        full_text_parts.append(body)
        direction = str(row.get("direction") or "").strip()
        normalized_body = _normalize_text(body)
        if direction == "Outbound":
            last_outbound = normalized_body
            continue
        if direction != "Inbound":
            continue

        inbound.append(row)
        customer_question = _looks_like_customer_question_instead_of_answer(body)
        continue_nudge = _is_continue_nudge_text(body)
        if details.business_type is None:
            details.business_type = _extract_business_type(normalized_body)
            if (
                details.business_type is None
                and _prompt_asked_business_type(last_outbound)
                and not customer_question
                and not continue_nudge
            ):
                details.business_type = _extract_business_type_short_answer(body)
        if details.business_name is None:
            details.business_name = _extract_business_name(body)
            if (
                details.business_name is None
                and _prompt_asked_business_name(last_outbound)
                and not customer_question
                and not continue_nudge
            ):
                details.business_name = _clean_short_answer(body).title() or None
        extracted_monthly_shipments = _extract_monthly_shipments(normalized_body)
        if (
            extracted_monthly_shipments is None
            and details.monthly_shipments is None
            and _prompt_asked_monthly_shipments(last_outbound)
            and not customer_question
            and not continue_nudge
        ):
            extracted_monthly_shipments = _extract_plain_quantity(normalized_body)
        if extracted_monthly_shipments is not None:
            details.monthly_shipments = extracted_monthly_shipments
        if details.aggregator_status is None:
            status, name, raw = _extract_aggregator(normalized_body)
            if not status and _prompt_asked_shipping_provider(last_outbound) and not customer_question and not continue_nudge:
                status, name, raw = _extract_provider_from_short_answer(body)
            details.aggregator_status = status
            details.aggregator_name = name
            details.aggregator_raw = raw
        if details.current_shipping_rate is None:
            details.current_shipping_rate = _extract_current_shipping_rate(normalized_body)
        if details.current_rate_zone is None:
            details.current_rate_zone = _extract_current_rate_zone(normalized_body)
        extracted_rto_percentage = _extract_rto_percentage(normalized_body)
        if (
            extracted_rto_percentage is None
            and _prompt_asked_rto_percentage(last_outbound)
            and not customer_question
            and not continue_nudge
        ):
            extracted_rto_percentage = _extract_plain_percentage(normalized_body)
        if extracted_rto_percentage is not None:
            details.rto_percentage = extracted_rto_percentage
        if details.pickup_city is None:
            details.pickup_city = _extract_pickup_city(body)
            if details.pickup_city is None and _prompt_asked_pickup_city(last_outbound) and not customer_question and not continue_nudge:
                details.pickup_city = _extract_city_short_answer(body)
        if details.delivery_city is None:
            details.delivery_city = _extract_delivery_city(body)
            if details.delivery_city is None and _prompt_asked_delivery_city(last_outbound) and not customer_question and not continue_nudge:
                details.delivery_city = _extract_city_short_answer(body)
        if details.average_weight is None:
            details.average_weight = _extract_average_weight(normalized_body)
            if details.average_weight is None and _prompt_asked_average_weight(last_outbound):
                details.average_weight = _extract_average_weight(normalized_body)
        if _asked_for_rate_quote(normalized_body):
            details.asked_for_rates = True
        if re.search(r"\b(sign\s*up|signup|register|account bana|account create|krwa do|karwa do)\b", normalized_body):
            details.signup_requested = True

    text = "\n".join(str(row.get("body") or "") for row in inbound)
    full_text = "\n".join(full_text_parts)
    normalized_full = _normalize_text(full_text)

    details.rate_shared = bool(re.search(r"\bshipkia rates?\b|₹\s*\d", normalized_full))
    details.latest_inbound_at = inbound[-1].get("creation") if inbound else None
    details.inbound_count = len(inbound)
    return details


def _prompt_asked_business_type(text: str) -> bool:
    return bool(
        re.search(r"\b(business|buisness).{0,35}(type|b2c|d2c|wholesale|retail|manufacturing|reseller)\b", text)
    )


def _prompt_asked_business_name(text: str) -> bool:
    return bool(re.search(r"\b(business|buisness|store|company|brand).{0,30}(name|naam)\b", text))


def _prompt_asked_monthly_shipments(text: str) -> bool:
    return bool(re.search(r"\b(monthly|mahine|mahina).{0,35}(shipment|shipments|order|orders)\b", text))


def _prompt_asked_shipping_provider(text: str) -> bool:
    return bool(re.search(r"\b(currently|current|abhi|kaunsa|which).{0,50}(shipping|aggregator|provider|courier)\b", text))


def _prompt_asked_rto_percentage(text: str) -> bool:
    return bool(
        re.search(
            r"\brto\b.{0,45}\b(percent|percentage|%)\b|\b(percent|percentage|%)\b.{0,45}\brto\b",
            text,
        )
    )


def _prompt_asked_pickup_city(text: str) -> bool:
    return bool(re.search(r"\bpickup.{0,25}(city|location|pincode)\b", text))


def _prompt_asked_delivery_city(text: str) -> bool:
    return bool(re.search(r"\bdelivery.{0,25}(city|location|pincode)\b", text))


def _prompt_asked_average_weight(text: str) -> bool:
    return bool(re.search(r"\b(average|avg|approx)?.{0,20}(weight|500g|shipment weight)\b", text))


def _extract_city_short_answer(text: str) -> str | None:
    value = _known_city_from_phrase(text)
    if value:
        return value.title()
    cleaned = _clean_short_answer(text)
    if not cleaned:
        return None
    if not _is_city_candidate(cleaned):
        return None
    return cleaned.title()


def _clean_short_answer(text: str) -> str:
    value = _clean_extracted_text(text)
    if not value:
        return ""
    normalized = _normalize_text(value)
    blocked = {
        "hello",
        "hi",
        "hey",
        "b2c",
        "d2c",
        "yes",
        "no",
        "nhi",
        "nahi",
        "nahin",
    }
    if normalized in blocked:
        return ""
    if re.search(r"\b(rto|rate|rates|shipment|shipments|aggregator|provider|courier|shipkia)\b", normalized):
        return ""
    if len(value) > 80:
        return ""
    return value


def _looks_like_customer_question_instead_of_answer(text: str) -> bool:
    raw = str(text or "").strip()
    normalized = _normalize_text(raw)
    if not normalized or _looks_like_crisp_sales_answer(normalized):
        return False
    question_words = (
        "kya",
        "kyu",
        "kyun",
        "kaise",
        "kaisa",
        "kesa",
        "kese",
        "kon",
        "kaun",
        "kaunsa",
        "kaunsi",
        "kitna",
        "kitne",
        "kab",
        "where",
        "what",
        "why",
        "how",
        "when",
        "which",
        "can",
        "do you",
        "does",
        "is it",
        "are you",
        "manage",
        "management",
        "workflow",
        "available",
        "provide",
    )
    return "?" in raw or any(re.search(rf"\b{re.escape(word)}\b", normalized) for word in question_words)


def _looks_like_crisp_sales_answer(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9.%\u0900-\u097F]+", " ", str(text or "").lower()).strip()
    if _extract_business_type(normalized):
        return True
    if re.fullmatch(r"(b2c|d2c|yes|yeah|yep|ha|haan|han|no|nhi|nahi|nahin)", normalized):
        return True
    if re.fullmatch(r"(?:around|approx|approximately|lagbhag)?\s*\d[\d,]*(?:\.\d+)?\s*(?:k|thousand|lakh|lac|%|percent|percentage|per|kg|kgs|g|gm|grams?)?", normalized):
        return True
    if re.fullmatch(r"(shiprocket|shipro|shipmozo|shipmoro|shipkaro|shipyaari|nimbuspost|pickrr|delhivery|ithink|ithink logistics|shipprime|ship prime)", normalized):
        return True
    return False


def _extract_plain_quantity(text: str) -> int | None:
    match = re.fullmatch(r"(?:around|approx|approximately|lagbhag)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac)?", text)
    if not match:
        return None
    return int(_scaled_number(match.group(1), match.group(2)))


def _extract_plain_percentage(text: str) -> float | None:
    match = re.fullmatch(
        r"(?:around|approx|approximately|lagbhag)?\s*(\d+(?:\.\d+)?)\s*(?:%|percent|percentage|per)?",
        text,
    )
    if not match:
        match = re.search(
            r"\b(\d+(?:\.\d+)?)\s*(?:%|percent|percentage|per)\b(?!\s*(?:day|din|month|mahina|week|hafte|shipment|shipments|order|orders)\b)",
            text,
        )
    if not match:
        match = re.search(
            r"\b(?:abhi|currently|current|btaya|bataya|bola|chal|chl|rha|raha|hai|h|to)\b.{0,30}?\b(\d+(?:\.\d+)?)\b",
            text,
        )
    if not match:
        return None
    value = float(match.group(1))
    return value if 0 <= value <= 100 else None


def _extract_provider_from_short_answer(text: str) -> Tuple[str | None, str | None, str | None]:
    status, name, raw = _extract_aggregator(_normalize_text(text))
    if status:
        return status, name, raw
    value = _clean_extracted_text(text)
    value = re.sub(
        r"\b(?:use|used|using|use\s+krta|use\s+karta|use\s+karte|abhi|currently|current|to|hai|hu|hoon|hum)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = _clean_extracted_text(value)
    if not value or _normalize_text(value) in {"shipping", "aggregator", "provider", "courier"}:
        return None, None, None
    if _looks_like_shipment_count_correction(value):
        return None, None, None
    return "Yes", value.title(), value


def _asked_for_rate_quote(text: str) -> bool:
    if not re.search(r"\b(rate|rates|price|pricing|charges|freight|shipping cost)\b", text):
        return False
    return bool(
        re.search(
            r"\b(?:batao|bataiye|bataye|batana|send|share|show|dikhao|kya|kitna|kitne|chaiye|chahiye|dijiye|dijea|card|list|zone\s*[a-f]|[a-f]\s*zone)\b",
            text,
        )
    )


def _extract_business_type(text: str) -> str | None:
    normalized = _normalize_text(text)
    for pattern, label in BUSINESS_TYPE_KEYWORDS:
        if re.search(pattern, normalized):
            return label
    return None


def _extract_business_type_short_answer(text: str) -> str | None:
    if _is_continue_nudge_text(text):
        return None
    extracted = _extract_business_type(text)
    if extracted:
        return extracted
    value = _clean_extracted_text(text)
    normalized = _normalize_text(value)
    if not normalized:
        return None
    blocked = {
        "yes",
        "yeah",
        "yep",
        "ha",
        "haan",
        "han",
        "no",
        "nhi",
        "nahi",
        "nahin",
        "none",
        "rate",
        "rates",
        "price",
        "pricing",
        "pickup",
        "delivery",
        "city",
        "shipkia",
    }
    if normalized in blocked or re.search(r"\b(rate|shipment|shipments|courier|aggregator|provider|rto)\b", normalized):
        return None
    if not re.search(r"[a-z]", normalized) or len(value) > 50:
        return None
    return value.title()


def _is_continue_nudge_text(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9\u0900-\u097F]+", " ", str(text or "").lower()).strip()
    return bool(
        re.fullmatch(
            r"(bolo|boliye|btao|batao|bataye|bataiye|haan bolo|ha bolo|yes tell|tell me|continue|next|aage|aage bolo|go ahead)",
            normalized,
        )
    )


def _extract_business_name(text: str) -> str | None:
    patterns = (
        r"(?:business|buisness|store|company|brand)(?:\s+ka)?\s+(?:name|naam)\s+(?:h|hai|is|:)?\s+(.+?)(?=\s+(?:and|aur)\s+(?:monthly|month|shipment|shipments|orders?|current|aggregator|shipping|rate|rto)\b|\s+(?:monthly|month|shipment|shipments|orders?|and\s+i|aur|current|aggregator|shipping|rate|rto)\b|[.,\n]|$)",
        r"(?:mera|mere|my)\s+(?:business|buisness|store|company|brand)\s+(?:ka\s+)?(?:name|naam)\s+(?:h|hai|is|:)?\s+(.+?)(?=\s+(?:and|aur)\s+(?:monthly|month|shipment|shipments|orders?|current|aggregator|shipping|rate|rto)\b|\s+(?:monthly|month|shipment|shipments|orders?|and\s+i|aur|current|aggregator|shipping|rate|rto)\b|[.,\n]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        name = _clean_extracted_text(match.group(1))
        if name:
            return name.title()
    return None


def _extract_monthly_shipments(text: str) -> int | None:
    daily = _extract_periodic_shipments(text, ("daily", "per day", "day", "din", "roz"), 30)
    if daily is not None:
        return daily

    weekly = _extract_periodic_shipments(text, ("weekly", "per week", "week", "hafte", "hafta"), 4)
    if weekly is not None:
        return weekly

    patterns = (
        r"(?:monthly|month|per month|mahine|mahina).{0,40}?(?:shipment|shipments|order|orders).{0,25}?(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac)?",
        r"(?:shipment|shipments|order|orders).{0,25}?(?:around|approx|approximately|lagbhag)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac)?",
        r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac)?\s*(?:monthly\s*)?(?:shipment|shipments|order|orders)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(_scaled_number(match.group(1), match.group(2) if match.lastindex and match.lastindex >= 2 else ""))
    return None


def _extract_periodic_shipments(text: str, period_terms: tuple[str, ...], multiplier: int) -> int | None:
    period_pattern = "|".join(re.escape(term) for term in period_terms)
    patterns = (
        rf"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac)?\s*(?:shipment|shipments|order|orders)?\s*(?:{period_pattern})\b",
        rf"(?:{period_pattern})\s*(?:shipment|shipments|order|orders)?.{{0,20}}?(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac)?",
        rf"(?:shipment|shipments|order|orders).{{0,20}}?(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac)?.{{0,20}}?(?:{period_pattern})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(_scaled_number(match.group(1), match.group(2) if match.lastindex and match.lastindex >= 2 else "") * multiplier)
    return None


def _looks_like_shipment_count_correction(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    return bool(
        re.search(r"\b(shipment|shipments|order|orders|monthly|month|daily|day|weekly|week|per day|per month|not monthly|din|roz)\b", normalized)
        and re.search(r"\d", normalized)
    )


def _extract_aggregator(text: str) -> Tuple[str | None, str | None, str | None]:
    negative = re.search(
        r"((?:not|no|nahi|nahin|without)\s+(?:using\s+)?(?:shipping\s+)?aggregator|"
        r"(?:aggregator|shiprocket|nimbuspost|pickrr|delhivery)\s+(?:use\s+)?(?:nahi|nahin|no|not))",
        text,
    )
    if negative:
        return "No", None, _clean_extracted_text(negative.group(1))

    known = {
        "shiprocket": "Shiprocket",
        "shipro": "Shipro",
        "shipmozo": "Shipmozo",
        "shipmoro": "Shipmozo",
        "ship prime": "Shipprime",
        "shipprime": "Shipprime",
        "nimbuspost": "NimbusPost",
        "pickrr": "Pickrr",
        "delhivery": "Delhivery",
        "shipyaari": "Shipyaari",
        "ithink logistics": "iThink Logistics",
        "ithink": "iThink Logistics",
    }
    for needle, label in known.items():
        if needle in text:
            return "Yes", label, label

    provider = re.search(r"\b([a-z][a-z0-9 ._-]{2,40}?)\s+(?:use|used|use kar|use kr|use krte|use karte|chalate)\b", text)
    if provider:
        name = _clean_extracted_text(provider.group(1))
        name = re.sub(r"^(?:me|main|mai|hum|we)\s+", "", name, flags=re.IGNORECASE).strip()
        if name and name not in {"shipping", "aggregator"}:
            return "Yes", name.title(), name

    generic = re.search(r"(?:using|use|current)\s+(?:shipping\s+)?aggregator\s+(?:is|:)?\s*([a-z0-9 ._-]{2,40})", text)
    if generic:
        name = _clean_extracted_text(generic.group(1))
        if name:
            return "Yes", name.title(), name
    return None, None, None


def _extract_current_shipping_rate(text: str) -> float | None:
    patterns = (
        r"(?:currently|current|abhi).{0,35}?(?:zone\s*)?[a-z]?\s*(\d+(?:\.\d+)?)\s*(?:ka|rs|rupees|₹|padta|pdta|parta)",
        r"(?:d\s*zone|zone\s*d).{0,15}?(\d+(?:\.\d+)?)",
        r"(?:rate|charges?|freight).{0,30}?(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*(?:ka|rs|rupees|₹)\s*(?:padta|pdta|parta)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    return None


def _extract_current_rate_zone(text: str) -> str | None:
    match = re.search(r"\b([a-z])\s*zone\b|\bzone\s*([a-z])\b", text)
    if not match:
        return None
    return str(match.group(1) or match.group(2) or "").upper()


def _extract_rto_percentage(text: str) -> float | None:
    patterns = (
        r"\brto\b.{0,30}?(\d+(?:\.\d+)?)\s*(?:%|percent|percentage|per)?",
        r"(\d+(?:\.\d+)?)\s*(?:%|percent|percentage|per)\s*(?:ka\s*)?\brto\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return float(match.group(1))
    return None


def _extract_average_weight(text: str) -> str | None:
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(kg|kgs|kilogram|g|gm|gram|grams)\b", text)
    if not match:
        return None
    unit = match.group(2)
    unit = "kg" if unit in {"kgs", "kilogram"} else "g" if unit in {"gm", "gram", "grams"} else unit
    return f"{match.group(1)} {unit}"


def _extract_pickup_city(text: str) -> str | None:
    route = _extract_route_cities(text)
    if route:
        return route[0]
    return _extract_labeled_city(text, "pickup")


def _extract_delivery_city(text: str) -> str | None:
    route = _extract_route_cities(text)
    if route:
        return route[1]
    return _extract_labeled_city(text, "delivery")


def _extract_route_cities(text: str) -> Tuple[str, str] | None:
    known_route = _extract_known_city_route(text)
    if known_route:
        return known_route

    matches = list(
        re.finditer(
            ROUTE_CONNECTOR_PATTERN,
            text,
            flags=re.IGNORECASE,
        )
    )
    if not matches:
        return None
    for match in reversed(matches):
        pickup, delivery = match.group(1), match.group(2)
        if not _looks_like_real_route_match(pickup, delivery, text):
            continue
        pickup_city, delivery_city = _clean_city(pickup), _clean_city(delivery)
        if _is_city_candidate(pickup_city) and _is_city_candidate(delivery_city):
            return pickup_city, delivery_city
    return None


def _extract_known_city_route(text: str) -> Tuple[str, str] | None:
    matches = list(
        re.finditer(
            r"([a-z][a-z .'-]{1,70}?)\s+(?:to|se)\s+([a-z][a-z .'-]{1,70})",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )
    for match in reversed(matches):
        pickup = _known_city_from_phrase(match.group(1))
        delivery = _known_city_from_phrase(match.group(2))
        if pickup and delivery:
            return pickup.title(), delivery.title()
    return None


def _extract_labeled_city(text: str, label: str) -> str | None:
    match = re.search(
        rf"\b{label}\s*(?:city|location)?\s*(?:is|hai|h|:|-)?\s*([a-z][a-z .'-]{{1,50}})",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    value = re.split(
        r"\s+(?:and|aur|delivery|pickup|monthly|shipment|shipments|order|orders|current|aggregator|shipping|rate|rates|rto|weight)\b",
        match.group(1),
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    city = _clean_city(value)
    return city if _is_city_candidate(city) else None


def _looks_like_real_route_match(pickup: str, delivery: str, full_text: str) -> bool:
    pickup_words = re.findall(r"[a-z]+", str(pickup or "").lower())
    delivery_words = re.findall(r"[a-z]+", str(delivery or "").lower())
    all_words = set(pickup_words + delivery_words)
    if not pickup_words or not delivery_words:
        return False
    if pickup_words[-1] in HINGLISH_TO_FILLERS:
        return False
    if all_words & NON_CITY_ROUTE_WORDS:
        return False
    normalized_full = _normalize_text(full_text)
    if re.search(r"\b(?:use|using|aggregator|provider|courier)\b", normalized_full):
        return bool(_known_city_from_phrase(pickup) and _known_city_from_phrase(delivery))
    return True


def _build_requirement_summary(details: ShipKiaLeadDetails) -> str:
    parts = []
    if details.asked_for_rates:
        parts.append("Customer asked for rates")
    if details.business_type:
        parts.append(f"Business type: {details.business_type}")
    if details.business_name:
        parts.append(f"Business/store: {details.business_name}")
    if details.monthly_shipments is not None:
        parts.append(f"Monthly shipments: {details.monthly_shipments}")
    if details.aggregator_status:
        aggregator = details.aggregator_name or details.aggregator_status
        parts.append(f"Current aggregator: {aggregator}")
    if details.current_shipping_rate is not None:
        zone = f" Zone {details.current_rate_zone}" if details.current_rate_zone else ""
        parts.append(f"Current shipping rate{zone}: {details.current_shipping_rate:g}")
    if details.rto_percentage is not None:
        parts.append(f"RTO percentage: {details.rto_percentage:g}%")
    if details.pickup_city and details.delivery_city:
        parts.append(f"Requested route: {details.pickup_city} to {details.delivery_city}")
    if details.average_weight:
        parts.append(f"Average weight: {details.average_weight}")
    if details.signup_requested:
        parts.append("Customer asked for signup/onboarding")
    return ". ".join(parts)[:1000]


def _qualification_status(details: ShipKiaLeadDetails) -> str:
    if details.business_type and details.business_name and (details.monthly_shipments or 0) >= 1000:
        return "Qualified"
    if details.collected_count:
        return "In Progress"
    return "New"


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def _clean_extracted_text(value: str) -> str:
    value = re.sub(r"\s+", " ", str(value or "").strip(" .,:;-_"))
    value = re.sub(r"\b(h|hai|is)$", "", value, flags=re.IGNORECASE).strip(" .,:;-_")
    return value


def _clean_city(value: str) -> str:
    value = _clean_extracted_text(value)
    value = re.sub(r"^(?:mujhe|muje|from)\s+", "", value, flags=re.IGNORECASE).strip()
    extracted = _known_city_from_phrase(value)
    if extracted:
        return extracted.title()
    value = re.split(
        r"\s+(?:kitna|kitne|rate|rates|charge|charges|pdega|padta|shipkia|pr|par|ka|ke|k|liye|lia|mein|me|cod|prepaid)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    alias = CITY_ALIASES.get(value.lower())
    if alias:
        return alias
    return value.title()


def _known_city_from_phrase(value: str) -> str | None:
    words = re.findall(r"[a-z]+", str(value or "").lower())
    if not words:
        return None
    known = {
        "delhi",
        "new delhi",
        "gurgaon",
        "gurugram",
        "noida",
        "ghaziabad",
        "faridabad",
        "mumbai",
        "pune",
        "nagpur",
        "thane",
        "bangalore",
        "bengaluru",
        "banglore",
        "chennai",
        "coimbatore",
        "hyderabad",
        "kolkata",
        "kolkta",
        "calcutta",
        "howrah",
        "lucknow",
        "kanpur",
        "agra",
        "varanasi",
        "jaipur",
        "udaipur",
        "jodhpur",
        "ahmedabad",
        "surat",
        "vadodara",
        "rajkot",
        "kochi",
        "cochin",
        "patna",
        "bhubaneswar",
        "indore",
        "bhopal",
        "chandigarh",
        "amritsar",
        "ludhiana",
        "dehradun",
        "ranchi",
        "raipur",
        "guwahati",
        "assam",
        "ladakh",
        "leh",
    }
    found = []
    for size in (3, 2, 1):
        for index in range(0, len(words) - size + 1):
            phrase = " ".join(words[index : index + size])
            alias = CITY_ALIASES.get(phrase.lower(), phrase)
            if phrase in known or alias.lower() in known:
                found.append(alias)
    return found[-1] if found else None


def _is_city_candidate(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    blocked_words = {
        "i",
        "want",
        "know",
        "about",
        "shipkia",
        "service",
        "services",
        "feature",
        "features",
        "workflow",
        "workflows",
        "ndr",
        "bdr",
        "rto",
        "cod",
        "saste",
        "cheap",
        "cheaper",
        "best",
        "low",
        "lower",
        "cost",
        "costs",
        "chaiye",
        "chahiye",
        "dijea",
        "dijiye",
        "aggregator",
        "provider",
        "current",
        "currently",
        "use",
        "using",
        "hu",
        "hoon",
        "hai",
        "krrha",
        "krra",
        "karta",
        "karte",
        "shipmoro",
        "shipro",
        "shiprocket",
        "shipmozo",
        "nimbuspost",
    }
    if set(text.split()) & blocked_words:
        return False
    return bool(re.fullmatch(r"[a-z][a-z .'-]{1,30}", text))


def _scaled_number(value: str, suffix: str | None = None) -> float:
    amount = float(str(value or "0").replace(",", ""))
    suffix = str(suffix or "").lower()
    if suffix in {"k", "thousand"}:
        amount *= 1000
    elif suffix in {"lakh", "lac"}:
        amount *= 100000
    return amount


def _policy_score(convo, history: List[Dict]) -> ScoreResult:
    bundle = get_conversation_policy(convo)
    scoring_policy = bundle.section("lead_scoring_policy") if bundle else {}
    if not scoring_policy.get("enabled"):
        return ScoreResult(
            lead_score=float(getattr(convo, "lead_score", 0) or 0),
            lead_temperature=str(getattr(convo, "lead_temperature", "") or ""),
            lead_lan=str(getattr(convo, "lead_lan", "") or ""),
            source="policy_disabled",
        )
    inbound = [h for h in history if h.get("direction") == "Inbound" and str(h.get("body") or "").strip()]
    latest_text = inbound[-1]["body"] if inbound else ""
    lang = resolve_language_from_history(
        str(latest_text or ""), history, channel_account=getattr(convo, "channel_account", None)
    )
    lead_lan = str(lang.get("label") or "").strip()

    return _policy_rule_score(convo, history, lead_lan, scoring_policy)


def _policy_rule_score(convo, history: List[Dict], lead_lan: str, policy: Dict) -> ScoreResult:
    score = float(policy.get("base_score") or 0)
    inbound_count = len([h for h in history if h.get("direction") == "Inbound" and str(h.get("body") or "").strip()])
    score += min(float(policy.get("inbound_message_cap") or 0), inbound_count * float(policy.get("inbound_message_weight") or 0))
    score += min(float(policy.get("unread_cap") or 0), int(convo.unread_count or 0) * float(policy.get("unread_weight") or 0))
    score += float((policy.get("priority_weights") or {}).get(convo.priority) or 0)

    joined = " ".join([str(h.get("body") or "") for h in history]).lower()
    hot_terms = [str(term).lower() for term in policy.get("hot_terms") or []]
    warm_terms = [str(term).lower() for term in policy.get("warm_terms") or []]
    score += sum(float(policy.get("hot_term_weight") or 0) for term in hot_terms if term in joined)
    score += sum(float(policy.get("warm_term_weight") or 0) for term in warm_terms if term in joined)

    score = _clamp(score, 0, 100)
    return ScoreResult(
        lead_score=score,
        lead_temperature=_score_to_temperature(score, policy),
        lead_lan=lead_lan,
        source="policy",
    )


def _score_to_temperature(score: float, policy: Dict) -> str:
    bands = sorted(
        (band for band in policy.get("temperature_bands") or [] if isinstance(band, dict)),
        key=lambda band: float(band.get("minimum") or 0),
        reverse=True,
    )
    for band in bands:
        if score >= float(band.get("minimum") or 0):
            return str(band.get("label") or "")
    return ""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
