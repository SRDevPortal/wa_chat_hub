# pyright: reportMissingImports=false, reportMissingModuleSource=false, reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportArgumentType=false, reportOperatorIssue=false, reportMissingTypeStubs=false, reportUnnecessaryCast=false

import json
import hashlib
import copy
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urljoin

import frappe
import requests  # type: ignore
from frappe.utils import add_to_date, cint, get_datetime, now_datetime
from frappe.utils.background_jobs import enqueue
from frappe.utils.password import get_decrypted_password
from frappe.utils.synchronization import filelock

from wa_chat_hub.ai.ocr_summary import (
    GENERIC_MEDIA_BODIES,
    build_media_context_for_chat,
)
from wa_chat_hub.ai.providers import CHAT_CAPABILITY, get_active_llm_provider_rows
from wa_chat_hub.ai.media_transcription import (
    TRANSCRIPT_CONTENT_TYPES,
    build_transcript_context_for_chat,
)
from wa_chat_hub.ai.service import create_ai_suggestion
from wa_chat_hub.agent_event import log_agent_event
from wa_chat_hub.api.vector_search import search_knowledge_base
from wa_chat_hub.outbound import send_outbound_message
from wa_chat_hub.prompts import (
    build_system_prompt_from_config,
    get_effective_prompt_config,
    get_multilingual_policy,
)
from wa_chat_hub.shipkia_aggregators import normalize_shipkia_aggregator
from wa_chat_hub.services import append_message
from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_get_value,
    safe_ai_insert,
    safe_ai_set_value,
    set_ai_security_context,
    set_service_user_context,
)
from wa_chat_hub.services import normalize_phone
from wa_chat_hub.task_logger import elapsed, queue_wait_seconds, task_log

CONVERSATION_HISTORY_LIMIT = 100
MAX_HISTORY_MESSAGE_CHARS = 1200
MEDIA_CONTENT_TYPES = frozenset({"Image", "Video", "Audio", "Document", "Sticker"})
SHIPKIA_RATE_TOOL_NAME = "calculate_shipkia_rate"
SHIPKIA_LEAD_DOCTYPE = "Lead"
SHIPKIA_ONBOARDING_SIGNUP_URL = "https://auth.shipkia.com/signup"
SHIPKIA_CLOSED_PROMPT_STATE = "__closed__"
SHIPKIA_WORKFLOW_LOOKBACK_SECONDS = 30 * 60
DEFAULT_TEXT_AUTOPILOT_REPLY_DELAY_SECONDS = 3
DEFAULT_MEDIA_AUTOPILOT_REPLY_DELAY_SECONDS = 15
AUTOPILOT_BATCH_LOCK_TIMEOUT = 180
AUTOPILOT_BATCH_LOOKBACK_MINUTES = 15
AUTOPILOT_MEDIA_BURST_LOOKBACK_MINUTES = 10
AUTOPILOT_MEDIA_BURST_GAP_SECONDS = 90
AUTOPILOT_MEDIA_SETTLE_SECONDS = 5
SHIPKIA_FRAGMENT_GRACE_SECONDS = 15
LOW_CONTEXT_INPUT_CHAR_BUDGET = 6500
LOW_CONTEXT_SYSTEM_CHAR_BUDGET = 4200
SHIPKIA_RATE_CITY_ALIASES = {
    "bangalore": "Bangalore",
    "bangaluru": "Bangalore",
    "banglore": "Bangalore",
    "bengaluru": "Bangalore",
    "benglore": "Bangalore",
    "bombay": "Mumbai",
    "dekhi": "Delhi",
    "dehradoon": "Dehradun",
    "dehradun": "Dehradun",
    "karela": "Kerala",
}
SHIPKIA_INVALID_RATE_CITY_WORDS = {
    "able",
    "aap",
    "bare",
    "brand",
    "business",
    "charges",
    "courier",
    "freight",
    "have",
    "ho",
    "how",
    "instagram",
    "in",
    "jaana",
    "kon",
    "krte",
    "kya",
    "mujhe",
    "know",
    "like",
    "marketplace",
    "need",
    "onboard",
    "onboarding",
    "offline",
    "okay",
    "orders",
    "partners",
    "price",
    "pricing",
    "provide",
    "rate",
    "rates",
    "g",
    "gm",
    "gms",
    "gram",
    "grams",
    "grm",
    "grms",
    "kg",
    "kgs",
    "kilogram",
    "kilograms",
    "shiprocket",
    "shipping",
    "share",
    "shopify",
    "social",
    "starting",
    "tell",
    "want",
    "website",
    "your",
    "you",
    "zone",
    "getting",
}
SHIPKIA_WORKFLOW_PROMPTS = {
    "business_type": (
        "Rates bataane ke liye ek quick detail chahiye: aapka business B2C hai ya D2C?"
    ),
    "business_name": "Aap business/store name, monthly shipments aur current aggregator ek message mein share kar dijiye.",
    "business_operation_mode": "Aap business/store name, monthly shipments aur current aggregator ek message mein share kar dijiye.",
    "current_aggregator_status": "Aap business/store name, monthly shipments aur current aggregator ek message mein share kar dijiye.",
    "current_aggregator_name": "Current aggregator ka naam kya hai?",
    "current_rate_rto": "Aapka approx current 500g shipping rate aur RTO % ek message mein share kar dijiye.",
    "current_shipping_rate": "Aapka approx current 500g shipping rate kya pad raha hai?",
    "rto_percentage": "Approx RTO % kya hai?",
    "monthly_shipments": "Approx monthly kitne shipments hote hain?",
}
SHIPKIA_REPLY_TEMPLATE_BLOCK_START = "SHIPKIA_WORKFLOW_REPLY_TEMPLATES"
SHIPKIA_REPLY_TEMPLATE_BLOCK_END = "END_SHIPKIA_WORKFLOW_REPLY_TEMPLATES"
SHIPKIA_DEFAULT_REPLY_TEMPLATES = {
    **SHIPKIA_WORKFLOW_PROMPTS,
    "pickup_delivery": "Pickup city, delivery city aur approx weight ek saath share kar dijiye. Example: Delhi to Mumbai, 500g.",
    "weight_grams": "Pickup city, delivery city aur approx weight ek saath share kar dijiye. Example: Delhi to Mumbai, 500g.",
    "weight_only": "Shipment weight kitna rahega? Example: 500g ya 1kg.",
    "payment_type": "Payment type Prepaid, COD ya dono rahega?",
    "order_value": "COD ke liye order value kya rahegi? Example: Rs 1000.",
    "greeting_known": "Hi {first_name}, welcome back to ShipKia. Kaise help kar sakti hoon?",
    "greeting_unknown": "Hi, welcome to ShipKia. Kaise help kar sakti hoon?",
    "route_starting_rate": (
        "ShipKia mein {pickup} se {delivery} ke liye {weight_grams}g shipment "
        "rates Rs {starting_rate} se start ho jaate hain. Exact rate pickup/delivery "
        "PIN aur serviceability ke hisaab se vary kar sakta hai."
    ),
    "soft_profile_after_rate": (
        "Agar aap comfortable hain, business/store name aur approx monthly shipments share kar dijiye; "
        "main uske hisaab se better courier options suggest kar dungi. Nahi batana ho toh koi baat nahi."
    ),
    "provider_fallback": "Message mil gaya. ShipKia team aapko shortly assist karegi.",
    "compact_profile": (
        "Aap business/store name, approx monthly shipments aur current aggregator ek message mein share kar dijiye. "
        "Example: ABC Store, 500 orders/month, using Shiprocket."
    ),
    "business_name_only": "Business/store name kya hai?",
    "current_aggregator_status_only": "Abhi aap kisi shipping aggregator ka use kar rahe hain? Yes/No",
    "profile_business_monthly": "Business/store name aur approx monthly shipments share kar dijiye.",
    "profile_business_aggregator": "Business/store name aur current aggregator share kar dijiye.",
    "profile_monthly_aggregator": "Approx monthly shipments aur current aggregator share kar dijiye.",
    "callback_time": "Sure, preferred callback time share kar dijiye.",
    "callback_saved": "Done, ShipKia team {callback_time} par callback karegi.",
    "acknowledgement": "Noted. Onboarding ya callback chahiye ho toh bata dijiye.",
    "onboarding_help": (
        "Bilkul, ShipKia onboarding/signup ke liye yeh link open kijiye: "
        f"{SHIPKIA_ONBOARDING_SIGNUP_URL}"
    ),
    "repeat_question_blocked": (
        "Noted. Main same detail dobara nahi poochungi. Jo information aapne share ki hai, "
        "main usi ke basis par continue karti hoon."
    ),
    "information_refused": (
        "Theek hai, main ye question dobara nahi poochungi. Rate calculate karne ke liye minimum "
        "pickup city, delivery city, weight aur payment type chahiye hota hai; jo details available hain unhi se continue karti hoon."
    ),
    "rate_card_request": (
        "Full rate card aur exact commercials team verification ke baad share kiye jaate hain. "
        "WhatsApp par main starting rates bata sakti hoon. Kya main ShipKia team se callback arrange kar du?"
    ),
    "custom_pricing_review": (
        "I understand. Is case mein ShipKia team custom pricing review kar sakti hai. "
        "Preferred callback time share kar dijiye, main team se connect karwa deti hoon."
    ),
    "frustration_recovery": (
        "Sorry, I understand this feels repetitive. Maine ab tak shared details note kar li hain. "
        "{next_question}"
    ),
    "exact_rate_callback_offer": (
        "Exact rate pickup/delivery PIN aur serviceability ke hisaab se confirm hota hai. "
        "Kya main ShipKia team se callback arrange kar du?"
    ),
}
SHIPKIA_RATE_PROMPT_MARKERS = {
    "pickup_delivery": (
        "pickup city aur delivery city",
        "pickup and delivery route",
        "route should i check",
        "pickup aur delivery",
        "pickup city or pincode",
        "delivery city or pincode",
        "pickup city, delivery city",
    ),
    "weight_grams": ("shipment weight kitna", "package weight", "weight kya", "weight share"),
    "weight_only": ("shipment weight kitna", "500g ya 1kg"),
    "payment_type": ("payment type", "prepaid, cod", "prepaid / cod", "prepaid cod"),
    "order_value": ("cod ke liye order value", "cod order value", "order value"),
    "callback_time": ("preferred callback time", "callback time share", "callback arrange"),
}
SHIPKIA_WORKFLOW_PROMPT_MARKERS = {
    "business_type": ("b2c hai ya d2c", "business b2c", "b2c/d2c", "b2c or d2c", "type of business"),
    "compact_profile": ("business/store name", "monthly shipments", "current aggregator"),
    "business_name": ("business name", "business/store name", "store name"),
    "business_operation_mode": ("orders mainly", "website, marketplace", "social media", "receive orders", "usually receive orders"),
    "current_aggregator_status": ("kisi shipping aggregator", "using any shipping aggregator", "currently using any shipping"),
    "current_aggregator_name": ("kaunsa shipping aggregator", "which aggregator", "aggregator are you currently using"),
    "current_rate_rto": ("current 500g shipping rate aur rto", "shipping rate aur rto", "current rate aur rto"),
    "current_shipping_rate": ("current shipping rate", "500g shipment ka current", "rate kitna pad", "what approx rate", "currently getting for a 500g"),
    "rto_percentage": ("rto percentage", "rto percent", "rto kitna"),
    "monthly_shipments": ("monthly kitne shipments", "har month handle", "shipments do you do", "orders per month"),
}
SHIPKIA_LEAD_CONTEXT_SLOTS = {
    "business_type",
    "business_name",
    "business_operation_mode",
    "current_aggregator_status",
    "current_aggregator_name",
    "current_shipping_rate",
    "rto_percentage",
    "monthly_shipments",
}
SHIPKIA_RATE_SLOTS = {
    "pickup_city",
    "delivery_city",
    "delivery_scope",
    "pickup_pincode",
    "delivery_pincode",
    "weight_grams",
    "payment_type",
    "order_value",
    "zone",
    "rate_type",
}


def _log_ai_timing(event: str, **fields) -> None:
    task_log("ai", event, **fields)


def _inside_append_message() -> bool:
    return bool(
        getattr(frappe.flags, "wa_chat_in_append_message", False)
        or getattr(frappe.local, "wa_chat_in_append_message", False)
    )


def _shipkia_channel_account_for_conversation(conversation: str | None) -> str:
    if not conversation:
        return ""
    try:
        return str(
            safe_ai_get_value("Chat Conversation", conversation, "channel_account")
            or ""
        )
    except Exception:
        return ""


def _shipkia_prompt_text_for_conversation(conversation: str | None) -> str:
    try:
        prompt_config = get_effective_prompt_config(
            _shipkia_channel_account_for_conversation(conversation)
        )
        return str(getattr(prompt_config, "system_prompt", "") or "")
    except Exception:
        return ""


def _shipkia_pending_slots_for_step(step: str, context: dict[str, Any] | None = None) -> list[str]:
    context = context or {}
    if step in {
        "business_name_only",
        "profile_business_monthly",
        "profile_business_aggregator",
        "profile_monthly_aggregator",
    }:
        return _shipkia_pending_slots_for_step("compact_profile", context)
    if step == "current_aggregator_status_only":
        return ["current_aggregator_status"]
    if step == "business_type":
        return ["business_type"]
    if step == "compact_profile":
        slots = []
        if not context.get("business_name"):
            slots.append("business_name")
        if not context.get("monthly_shipments"):
            slots.append("monthly_shipments")
        if not context.get("current_aggregator_status"):
            slots.append("current_aggregator_status")
        if context.get("current_aggregator_status") == "Yes" and not context.get("current_aggregator_name"):
            slots.append("current_aggregator_name")
        return slots or ["business_name", "monthly_shipments", "current_aggregator_status"]
    if step == "current_aggregator_name":
        return ["current_aggregator_name"]
    if step == "current_aggregator_status":
        return ["current_aggregator_status"]
    if step in {"current_rate_rto", "current_shipping_rate", "rto_percentage"}:
        slots = []
        if not _shipkia_has_positive_number(context.get("current_shipping_rate")):
            slots.append("current_shipping_rate")
        if not _shipkia_has_positive_number(context.get("rto_percentage")):
            slots.append("rto_percentage")
        return slots or ["current_shipping_rate", "rto_percentage"]
    if step == "monthly_shipments":
        return ["monthly_shipments"]
    if step in {"pickup_delivery", "weight_grams", "weight_only"}:
        rate_details = context.get("rate_details") or {}
        slots = []
        if not (rate_details.get("pickup_city") or rate_details.get("pickup_pincode")):
            slots.append("pickup_city")
        if not (
            rate_details.get("delivery_city")
            or rate_details.get("delivery_pincode")
            or rate_details.get("delivery_scope")
        ):
            slots.append("delivery_city")
        if not rate_details.get("weight_grams"):
            slots.append("weight_grams")
        return slots or ["pickup_city", "delivery_city", "weight_grams"]
    if step == "payment_type":
        return ["payment_type"]
    if step == "order_value":
        return ["order_value"]
    if step == "callback_time":
        return ["callback_time"]
    return []


def _shipkia_load_conversation_state(conversation: str | None) -> dict[str, Any]:
    if not conversation or not safe_ai_exists("Chat Conversation", conversation):
        return {}
    meta = frappe.get_meta("Chat Conversation")
    fields = [
        fieldname
        for fieldname in ("shipkia_last_bot_question", "shipkia_pending_slots")
        if meta.has_field(fieldname)
    ]
    if not fields:
        return {}
    values = safe_ai_get_value("Chat Conversation", conversation, fields, as_dict=True) or {}
    pending_slots_raw = str(values.get("shipkia_pending_slots") or "").strip()
    pending_slots: list[str] = []
    pending_confirmation: dict[str, Any] = {}
    if pending_slots_raw:
        try:
            parsed = json.loads(pending_slots_raw)
            if isinstance(parsed, list):
                pending_slots = [str(slot) for slot in parsed if str(slot)]
            elif isinstance(parsed, dict):
                pending_confirmation = parsed.get("pending_confirmation") if isinstance(parsed.get("pending_confirmation"), dict) else {}
        except Exception:
            pending_slots = [slot.strip() for slot in pending_slots_raw.split(",") if slot.strip()]
    latest_prompt = str(values.get("shipkia_last_bot_question") or "").strip()
    state_closed = latest_prompt == SHIPKIA_CLOSED_PROMPT_STATE
    return {
        "latest_prompt": "" if state_closed else latest_prompt,
        "pending_slots": pending_slots,
        "pending_confirmation": pending_confirmation,
        "state_closed": state_closed,
    }


def _shipkia_save_conversation_state(
    conversation: str | None,
    step: str,
    context: dict[str, Any] | None = None,
) -> None:
    if not conversation or not safe_ai_exists("Chat Conversation", conversation):
        return
    meta = frappe.get_meta("Chat Conversation")
    updates: dict[str, Any] = {}
    if meta.has_field("shipkia_last_bot_question"):
        updates["shipkia_last_bot_question"] = step or SHIPKIA_CLOSED_PROMPT_STATE
    if meta.has_field("shipkia_pending_slots"):
        pending_slots = _shipkia_pending_slots_for_step(step, context)
        updates["shipkia_pending_slots"] = json.dumps(pending_slots) if pending_slots else ""
    if updates:
        safe_ai_set_value("Chat Conversation", conversation, updates, update_modified=False)


def _shipkia_save_pending_confirmation(
    conversation: str | None,
    decision: dict[str, Any],
) -> None:
    if not conversation or not safe_ai_exists("Chat Conversation", conversation):
        return
    meta = frappe.get_meta("Chat Conversation")
    updates: dict[str, Any] = {}
    if meta.has_field("shipkia_last_bot_question"):
        updates["shipkia_last_bot_question"] = "confirm_update"
    if meta.has_field("shipkia_pending_slots"):
        updates["shipkia_pending_slots"] = json.dumps(
            {
                "pending_confirmation": {
                    "entities": decision.get("entities") or {},
                    "field_confidence": decision.get("field_confidence") or {},
                }
            }
        )
    if updates:
        safe_ai_set_value("Chat Conversation", conversation, updates, update_modified=False)


def _shipkia_templates_from_prompt(prompt: str) -> dict[str, str]:
    if not prompt or SHIPKIA_REPLY_TEMPLATE_BLOCK_START not in prompt:
        return {}
    try:
        block = prompt.split(SHIPKIA_REPLY_TEMPLATE_BLOCK_START, 1)[1]
        block = block.split(SHIPKIA_REPLY_TEMPLATE_BLOCK_END, 1)[0]
    except Exception:
        return {}

    templates: dict[str, str] = {}
    current_key = ""
    current_value: list[str] = []

    def flush_current() -> None:
        nonlocal current_key, current_value
        if current_key:
            value = "\n".join(current_value).strip().strip('"').strip("'")
            if value:
                templates[current_key] = value.replace("\\n", "\n")
        current_key = ""
        current_value = []

    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            if current_key and current_value:
                current_value.append("")
            continue
        match = re.match(r"^([a-zA-Z0-9_]+)\s*[:=]\s*(.*)$", line)
        if match:
            flush_current()
            current_key = match.group(1).strip()
            current_value = [match.group(2).strip()]
            continue
        if current_key:
            current_value.append(raw_line.rstrip())
    flush_current()
    return templates


def _shipkia_reply_templates(conversation: str | None) -> dict[str, str]:
    templates = dict(SHIPKIA_DEFAULT_REPLY_TEMPLATES)
    templates.update(
        _shipkia_templates_from_prompt(
            _shipkia_prompt_text_for_conversation(conversation)
        )
    )
    return templates


def _shipkia_format_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _shipkia_template_values(
    context: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, str]:
    source = {**(context or {}), **(extra or {})}
    values = {
        key: _shipkia_format_value(value)
        for key, value in source.items()
        if not isinstance(value, (dict, list, tuple))
    }
    customer_name = values.get("customer_name") or ""
    values.setdefault("first_name", customer_name.split()[0] if customer_name else "")
    values.setdefault("business_type", "")
    values.setdefault("business_name", "")
    values.setdefault("monthly_shipments", "")
    values.setdefault("aggregator_name", values.get("current_aggregator_name", ""))
    values.setdefault("aggregator_summary", "")
    values.setdefault("pickup", "")
    values.setdefault("delivery", "")
    values.setdefault("weight_grams", "")
    values.setdefault("payment_label", "")
    values.setdefault("starting_rate", "")
    values.setdefault("rate_lines", "")
    return values


def _shipkia_render_reply_template(
    key: str,
    conversation: str | None,
    context: dict[str, Any] | None = None,
    **extra: Any,
) -> str:
    template = _shipkia_reply_templates(conversation).get(key, "")
    if not template:
        return ""
    values = _shipkia_template_values(context, extra)
    try:
        return template.format_map(defaultdict(str, values)).strip()
    except Exception:
        return template.strip()


def _normalise_rate_city(value: str) -> str:
    text = re.sub(r"[^a-zA-Z\s]+", " ", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\b(?:k|ke|ki|ka)$", "", text, flags=re.IGNORECASE).strip()
    return SHIPKIA_RATE_CITY_ALIASES.get(text.lower(), text.title())


def _strip_shipkia_rate_noise(value: str) -> str:
    text = re.sub(
        r"\b\d+(?:\.\d+)?\s*(?:kg|kgs|kilogram|kilograms|kilo|kilos|g|gr|gm|gms|grm|grms|gram|grams)\b",
        " ",
        value or "",
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(?:prepaid|pre\s*paid|cod|cash\s*on\s*delivery|rate|rates|price|pricing|charge|charges|shipping|courier|cost|flat)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", text).strip(" ,.-")


def _rate_location_key(value: str) -> str:
    text = re.sub(r"[^a-zA-Z\s]+", " ", value or "").strip().casefold()
    return re.sub(r"\s+", " ", text)


def _looks_like_rate_city(value: str) -> bool:
    text = _rate_location_key(value)
    tokens = [token for token in text.split() if token]
    if not tokens:
        return False
    if any(token in SHIPKIA_INVALID_RATE_CITY_WORDS for token in tokens):
        return False
    return True


def _shipkia_looks_like_current_rate_answer(text: str) -> bool:
    lower = (text or "").casefold()
    if not lower:
        return False
    has_zone_rate = bool(re.search(r"\bzone\s*[a-f]\b", lower))
    has_current_rate_language = bool(
        re.search(
            r"\b(starting|getting|get|current|currently|abhi|mil|pad|rate|charges?)\b",
            lower,
        )
    )
    has_route_language = bool(re.search(r"\b(from\s+[a-zA-Z]|[a-zA-Z]+\s+(?:to|se)\s+[a-zA-Z]+)\b", lower))
    return has_zone_rate and has_current_rate_language and not has_route_language


def _shipkia_exact_rate_intent(text: str) -> bool:
    lower = (text or "").casefold()
    if not lower:
        return False
    return bool(
        re.search(
            r"\b(exact|final|proper|confirmed|confirm|accurate|pin\s*code|pincode|serviceability)\b",
            lower,
        )
        and re.search(r"\b(rate|rates|price|pricing|charge|charges|freight)\b", lower)
    )


def _shipkia_onboarding_intent(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(r"\b(onboard|onboarding|sign\s*up|signup|register|registration|account setup|account banana|join shipkia)\b", lower)
    )


def _shipkia_rate_card_request(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(r"\b(rate card|ratecard|full rates?|full pricing|complete pricing|commercials?|price list|all rates?)\b", lower)
    )


def _shipkia_price_objection_intent(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(
            r"\b(too high|very high|high rates?|expensive|costly|mehenga|mahinga|mehnga|zyada|jyada|jada|jda|bahut zyada|rate high|rates high|price high|pricing high|paise zyada|paise jyada|paise jada|paise jda)\b",
            lower,
        )
    )


def _shipkia_customer_memory_intent(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(
            r"\b(do you remember|remember my|mera|meri|my)\b",
            lower,
        )
        and re.search(
            r"\b(brand|business|store|company|name|details|info|information|aggregator|shipments|rate|rto)\b",
            lower,
        )
    ) or bool(
        re.search(
            r"\b(what details do you have|what information do you have|mere details|meri details|mera data|meri information)\b",
            lower,
        )
    )


def _shipkia_customer_memory_reply(text: str, context: dict[str, Any], conversation: str | None = None) -> str:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    business_name = str(context.get("business_name") or "").strip()
    aggregator = str(context.get("current_aggregator_name") or "").strip()
    monthly_shipments = context.get("monthly_shipments")
    current_rate = context.get("current_shipping_rate")
    rto_percentage = context.get("rto_percentage")

    if re.search(r"\b(brand|business|store|company|name)\b", lower):
        if business_name:
            return f"Haan, aapka business/store name {business_name} saved hai."
        return "Mujhe abhi aapka business/store name saved nahi dikh raha. Aap share kar denge toh main note kar lungi."

    if "aggregator" in lower:
        if aggregator:
            return f"Haan, current aggregator {aggregator} saved hai."
        return "Mujhe abhi current aggregator saved nahi dikh raha. Aap aggregator name share kar dijiye."

    details = []
    if business_name:
        details.append(f"business/store name {business_name}")
    if aggregator:
        details.append(f"current aggregator {aggregator}")
    if monthly_shipments not in (None, "", 0, "0"):
        details.append(f"monthly shipments approx {_shipkia_format_value(monthly_shipments)}")
    if current_rate not in (None, "", 0, "0"):
        details.append(f"current 500g rate Rs {_shipkia_format_value(current_rate)}")
    if rto_percentage not in (None, "", 0, "0"):
        details.append(f"RTO {_shipkia_format_value(rto_percentage)}%")
    if details:
        return "Haan, mere paas yeh details saved hain: " + ", ".join(details) + "."
    return "Mujhe abhi aapki ShipKia lead details saved nahi dikh rahi. Aap business name, monthly shipments aur current aggregator share kar dijiye."


def _shipkia_feature_question_intent(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(
            r"\b(features?|services?|kya kya|ky ky|what can you do|rto|ndr|address validation|order confirmation|customer decline|decline the order|cancel|courier partners?|rate comparison|ivr|whatsapp engagement|mobile number|address update)\b",
            lower,
        )
    )


def _shipkia_feature_reply(text: str) -> str:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()

    if re.search(r"\b(decline|reject|mana|cancel|automatically cancel|auto cancel|directly cancel)\b", lower):
        return (
            "Nahi, agar customer order decline karta hai toh ShipKia automatically order cancel nahi karta. "
            "Woh response dashboard par show hota hai, jisse seller apne hisaab se action le sakta hai."
        )

    if re.search(r"\b(order confirmation|confirm order|confirmation|whatsapp engagement|ivr)\b", lower):
        return (
            "Haan, ShipKia order confirmation mein help karta hai. Customer se WhatsApp engagement aur IVR call ke through "
            "confirmation li ja sakti hai. Agar customer decline karta hai, order auto-cancel nahi hota; response dashboard "
            "par show hota hai taaki seller final action le sake."
        )

    if re.search(r"\b(ndr|rto|non delivery|address validation|address update|mobile number|phone update|customer access)\b", lower):
        return (
            "ShipKia mein NDR flow se RTO control karne mein help milti hai. Seller dashboard se NDR cases track kar sakta hai, "
            "aur end customer tak WhatsApp aur IVR call ke through directly reach kiya ja sakta hai. Customer zarurat padne par "
            "apna mobile number ya address update bhi kar sakta hai, jisse delivery success improve hoti hai."
        )

    if re.search(r"\b(courier|partner|partners|rate comparison|compare|comparison|better rate|rates?)\b", lower):
        return (
            "ShipKia multiple courier partners ke saath shipping support karta hai, jisse seller better rate comparison kar sakta hai "
            "aur shipment ke liye suitable courier option choose kar sakta hai. Rates route, weight, mode aur serviceability ke hisaab se vary karte hain."
        )

    return (
        "ShipKia mein multiple courier partners, better rate comparison, order confirmation via WhatsApp engagement aur IVR call, "
        "NDR flow for RTO control, customer WhatsApp/IVR reach, aur customer-side mobile number/address update support milta hai. "
        "Agar customer order decline karta hai toh order automatically cancel nahi hota; dashboard par response show hota hai taaki seller action le sake."
    )


def _shipkia_confirmation_yes(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip(" .,!।")
    return bool(
        re.fullmatch(
            r"(yes|yep|yeah|haan|han|ha|hn|ji|hanji|ok|okay|okk|confirm|confirmed|save|save it|kar do|kr do|kardo|sahi hai|right)",
            lower,
        )
    )


def _shipkia_confirmation_no(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip(" .,!।")
    return bool(re.fullmatch(r"(no|nahi|nahin|na|wrong|galat|mat karo|dont save|don't save|cancel)", lower))


def _shipkia_negotiation_intent(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(
            r"\b(negotiate|negotiation|negotiable|discount|lower rate|lower rates|better rate|better rates|best rate|best rates|custom pricing|special pricing|can you reduce|reduce rates?|rate kam|kam rate|aur kam)\b",
            lower,
        )
    )


def _shipkia_frustration_intent(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(
            r"\b(not reading|not getting|already told|just told|told you|again n again|again and again|baar baar|repeat|repeating|wrong with you|fuck|stupid|irritat|annoy|one thing at a time|ek baar mein ek cheez|ek bar mein ek cheez)\b",
            lower,
        )
    )


def _shipkia_callback_yes(text: str) -> bool:
    lower = (text or "").casefold().strip()
    return bool(
        re.search(
            r"\b(yes|yep|haan|ha|hn|han|hanji|haji|sure|ok|okay|please|kar do|kr do|krdo|kardo|arrange|call kara|callback)\b",
            lower,
        )
    )


def _shipkia_callback_no(text: str) -> bool:
    lower = (text or "").casefold().strip()
    return bool(re.search(r"\b(no|nahi|nahin|na|not now|mat|don't)\b", lower))


def _shipkia_pan_india_scope(text: str) -> str:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if re.search(r"\b(pan\s*india|all\s*india|across\s*india|india\s*wide|nationwide)\b", lower):
        return "Pan India"
    return ""


def _shipkia_pickup_city_for_pan_india(text: str) -> str:
    value = re.sub(r"\b(pan\s*india|all\s*india|across\s*india|india\s*wide|nationwide)\b", " ", str(text or ""), flags=re.IGNORECASE)
    value = re.sub(
        r"\b(pre\s*paid|prepaid|cod|both|dono|rate|rates|price|pricing|charges?|shipping|courier|cost|pickup|delivery|destination|from|to|se|for|mujhe|bata|bta|tell|need|want)\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(r"\b\d+(?:\.\d+)?\s*(?:kg|kgs|kilogram|kilograms|g|gm|gms|grm|grms|gram|grams)\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"(?<!\d)[1-9]\d{5}(?!\d)", " ", value)
    value = re.sub(r"[^a-zA-Z\s]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    city = _normalise_rate_city(value)
    return city if city and _looks_like_rate_city(city) else ""


def _shipkia_extract_callback_time(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value or _shipkia_callback_yes(value) and len(value.split()) <= 3:
        return ""
    if _shipkia_callback_no(value):
        return ""
    lower = value.casefold()
    has_callback_time = bool(
        re.search(
            r"\b(?:[0-2]?\d(?::[0-5]\d)?\s*(?:am|pm)|[0-2]?\d\s*baje|today|tomorrow|kal|aaj|parso|morning|afternoon|evening|night|shaam|subah|dopahar|raat|monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun|abhi|now)\b",
            lower,
        )
        or bool(re.search(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", lower))
    )
    if not has_callback_time:
        return ""
    return value[:120]


def _shipkia_acknowledgement(text: str) -> bool:
    lower = re.sub(r"\s+", " ", text or "").casefold().strip(" .,!।")
    return bool(
        re.fullmatch(
            r"(ok|okay|okk|acha|accha|theek|thik|fine|done|noted|thanks|thank you|dhanyawad|shukriya|alright|cool|great|nice|haan|ha|hn|yes)",
            lower,
        )
    )


def _shipkia_parse_aggregator_name(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return ""
    lower = value.casefold()
    if re.search(r"\b(no|nahi|nahin|not using|koi nahi|none|self|khud)\b", lower):
        return ""

    known = normalize_shipkia_aggregator(value)
    if known:
        return known

    match = re.search(
        r"\b(?:using|use|with|through|on|currently using|aggregator(?: is)?|partner(?: is)?)\s+([a-zA-Z0-9][a-zA-Z0-9 &._-]{1,40})",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        name = re.split(
            r"\b(?:right now|abhi|and|,|\.|rate|rates|rto|orders?|shipments?|monthly|per month)\b",
            match.group(1),
            flags=re.IGNORECASE,
        )[0].strip(" ,.-")
        if re.search(
            r"\b(shopify|instagram|offline|online|website|marketplace|social|whatsapp|manual)\b",
            name,
            flags=re.IGNORECASE,
        ):
            return ""
        known = normalize_shipkia_aggregator(name)
        if known:
            return known
        if name:
            return name[:60]
    return ""


def _shipkia_non_business_free_text(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip(" .,!।?")
    if not lower:
        return True
    return bool(
        re.search(
            r"\b(jldi|jaldi|jald[iy]|quick|fast|bolo|btao|batao|tell|reply|wait|ruk|samjho|samajh|one thing|ek baar|ek bar|cheez|kro|karo|ok|okay|acha|accha|theek|thik)\b",
            lower,
        )
    )


def _shipkia_possible_business_name(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip(" .,!।?")
    if not value or len(value) > 60:
        return ""
    lower = value.casefold()
    if _shipkia_non_business_free_text(value):
        return ""
    if re.search(
        r"\b(what|who|how|when|where|why|can|could|would|should|rate|rates|price|pricing|shipping|pickup|delivery|kg|gms?|prepaid|cod|callback|call|help|services?)\b",
        lower,
    ):
        return ""
    if _shipkia_parse_business_type(value) or _shipkia_parse_aggregator_name(value):
        return ""
    if _shipkia_parse_monthly_shipments(value, expected=False) is not None:
        return ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 &.'-]{2,59}", value):
        return ""
    return value


def _shipkia_extract_business_name(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value:
        return ""
    if _shipkia_non_business_free_text(value):
        return ""
    value = re.sub(r"^\s*(?:b\s*2\s*c|b2c|d\s*2\s*c|d2c)\s*[,;:-]\s*", "", value, flags=re.IGNORECASE)
    patterns = (
        r"\b(?:business|store|brand|company|shop)\s*(?:name\s*)?(?:is|hai|:|-)?\s+(.+)$",
        r"\bmy\s+(?:business|store|brand|company|shop)\s+(?:is|name is|called)?\s*(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if not match:
            continue
        name = re.split(
            r"\b(?:and|aur|with|using|currently|current|monthly|orders?|shipments?|per month|aggregator|rto|rate|rates)\b|,",
            match.group(1),
            flags=re.IGNORECASE,
        )[0].strip(" ,.-")
        if name and not re.search(r"\b(orders?|shipments?|using|aggregator|rate|rto)\b", name, flags=re.IGNORECASE):
            return name[:120]

    first_part = re.split(
        r",|;|\b(?:aur|and)\s+(?:approx\s+)?(?:monthly|mahine|shipments?|orders?)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ,.-")
    if (
        first_part
        and len(first_part) <= 80
        and not _shipkia_parse_business_type(first_part)
        and not _shipkia_parse_monthly_shipments(first_part, expected=False)
        and not _shipkia_parse_aggregator_name(first_part)
        and not _shipkia_non_business_free_text(first_part)
        and not re.search(r"\b(yes|no|haan|nahi|using|aggregator|orders?|shipments?|rate|rto|zone)\b", first_part, flags=re.IGNORECASE)
    ):
        return first_part
    return ""


def _shipkia_extract_compact_profile(text: str, expected: bool = False) -> dict[str, Any]:
    profile: dict[str, Any] = {}
    business_type = _shipkia_parse_business_type(text)
    if business_type:
        profile["business_type"] = business_type

    business_name = _shipkia_extract_business_name(text) if expected else ""
    if business_name:
        profile["business_name"] = business_name

    monthly_shipments = _shipkia_parse_monthly_shipments(text, expected=expected)
    if monthly_shipments is not None:
        profile["monthly_shipments"] = monthly_shipments

    yes_no = _shipkia_parse_yes_no(text)
    aggregator_name = _shipkia_parse_aggregator_name(text)
    if aggregator_name:
        profile["current_aggregator_status"] = "Yes"
        profile["current_aggregator_name"] = aggregator_name
    elif yes_no:
        profile["current_aggregator_status"] = yes_no

    current_rate = _shipkia_parse_current_shipping_rate(text, expected=False)
    if current_rate is not None:
        profile["current_shipping_rate"] = current_rate
    rto_percentage = _shipkia_parse_rto_percentage(text, expected=False)
    if rto_percentage is not None:
        profile["rto_percentage"] = rto_percentage
    return profile


def _shipkia_extract_pending_slot_updates(text: str, pending_slots: list[str] | tuple[str, ...]) -> dict[str, Any]:
    slots = set(pending_slots or [])
    if not slots:
        return {}

    updates: dict[str, Any] = {}
    negative_aggregator_answer = _shipkia_negative_aggregator_answer(text)
    if "business_type" in slots:
        value = _shipkia_parse_business_type(text)
        if value:
            updates["business_type"] = value

    if "business_name" in slots:
        value = "" if negative_aggregator_answer else (_shipkia_extract_business_name(text) or _shipkia_safe_free_text(text))
        if value:
            updates["business_name"] = value

    if "monthly_shipments" in slots:
        value = _shipkia_parse_monthly_shipments(text, expected=True)
        if value is not None:
            updates["monthly_shipments"] = value

    if "current_aggregator_status" in slots:
        aggregator_name = _shipkia_parse_aggregator_name(text)
        if aggregator_name:
            updates["current_aggregator_status"] = "Yes"
            updates["current_aggregator_name"] = aggregator_name
        else:
            value = _shipkia_parse_yes_no(text)
            if value:
                updates["current_aggregator_status"] = value

    if "current_aggregator_name" in slots:
        if negative_aggregator_answer or _shipkia_parse_yes_no(text) == "No":
            updates["current_aggregator_status"] = "No"
            value = ""
        else:
            value = _shipkia_parse_aggregator_name(text) or _shipkia_safe_free_text(text)
        if value:
            updates["current_aggregator_name"] = value
            updates.setdefault("current_aggregator_status", "Yes")

    if {"current_shipping_rate", "rto_percentage"} & slots:
        rate_value, rto_value = _shipkia_parse_rate_rto_pair(text)
        if "current_shipping_rate" in slots and rate_value is not None:
            updates["current_shipping_rate"] = rate_value
        if "rto_percentage" in slots and rto_value is not None:
            updates["rto_percentage"] = rto_value
        elif slots == {"rto_percentage"}:
            value = _shipkia_parse_number_answer(text)
            if value is not None and 0 <= value <= 100:
                updates["rto_percentage"] = value

    rate_details = _extract_shipkia_rate_details(text)
    for slot in slots & SHIPKIA_RATE_SLOTS:
        if rate_details.get(slot):
            updates.setdefault("rate_details", {})[slot] = rate_details[slot]
    if slots & {"pickup_city", "delivery_city", "pickup_pincode", "delivery_pincode"}:
        if not updates.get("rate_details"):
            location = _shipkia_single_location_answer(text)
            if location:
                updates["rate_details"] = {}
                if location.get("pincode"):
                    if "pickup_pincode" in slots:
                        updates["rate_details"]["pickup_pincode"] = location["pincode"]
                    elif "delivery_pincode" in slots:
                        updates["rate_details"]["delivery_pincode"] = location["pincode"]
                elif location.get("city"):
                    if "pickup_city" in slots:
                        updates["rate_details"]["pickup_city"] = location["city"]
                    elif "delivery_city" in slots:
                        updates["rate_details"]["delivery_city"] = location["city"]

    return updates


def _shipkia_apply_slot_updates(context: dict[str, Any], updates: dict[str, Any]) -> None:
    for key, value in (updates or {}).items():
        if key == "rate_details":
            context.setdefault("rate_details", {})
            for rate_key, rate_value in (value or {}).items():
                if rate_value not in (None, ""):
                    context["rate_details"][rate_key] = rate_value
        elif value not in (None, ""):
            context[key] = value
    _shipkia_clear_aggregator_dependent_context(context)


def _shipkia_clear_aggregator_dependent_context(context: dict[str, Any]) -> None:
    if str(context.get("current_aggregator_status") or "").casefold() != "no":
        return
    for key in ("current_aggregator_name", "current_shipping_rate", "rto_percentage"):
        context.pop(key, None)


def _shipkia_context_has_customer_update(context: dict[str, Any]) -> bool:
    if any(context.get(slot) not in (None, "") for slot in SHIPKIA_LEAD_CONTEXT_SLOTS):
        return True
    rate_details = context.get("rate_details") or {}
    return any(rate_details.get(slot) not in (None, "") for slot in SHIPKIA_RATE_SLOTS)


def _shipkia_text_has_customer_update(text: str, rate_details: dict[str, Any]) -> bool:
    if _shipkia_parse_business_type(text):
        return True
    profile = _shipkia_extract_compact_profile(text, expected=True)
    if any(
        profile.get(slot) not in (None, "")
        for slot in (
            "business_name",
            "monthly_shipments",
            "current_aggregator_status",
            "current_aggregator_name",
        )
    ):
        return True
    if (
        _shipkia_parse_current_shipping_rate(text, expected=True) is not None
        or _shipkia_parse_rto_percentage(text, expected=True) is not None
    ):
        return True
    return any((rate_details or {}).get(slot) not in (None, "") for slot in SHIPKIA_RATE_SLOTS)


def _shipkia_correction_intent(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(
            r"\b(no|nahi|nahin|actually|actully|correction|correct|update|change|galat|wrong|not)\b",
            lower,
        )
        and re.search(
            r"\b(brand|business|store|company|name|monthly|shipments?|orders?|aggregator|rate|rto|pickup|delivery|weight|payment)\b",
            lower,
        )
    ) or bool(
        re.search(r"\b(no|nahi|nahin|actually|actully|not|galat|wrong)\b", lower)
        and normalize_shipkia_aggregator(text)
    )


def _shipkia_extract_correction_entities(text: str) -> tuple[dict[str, Any], dict[str, float]]:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    lower = value.casefold()
    entities: dict[str, Any] = {}
    confidence: dict[str, float] = {}

    business_match = re.search(
        r"\b(?:brand|business|store|company)(?:\s+name)?\s*(?:is|hai|=|:)?\s+(.+)$",
        value,
        flags=re.IGNORECASE,
    )
    if business_match:
        business_name = _shipkia_extract_business_name(business_match.group(0))
        if business_name:
            entities["business_name"] = business_name
            confidence["business_name"] = 0.95

    if "monthly" in lower or "shipment" in lower or "order" in lower:
        monthly_shipments = _shipkia_parse_monthly_shipments(value, expected=True)
        if monthly_shipments is not None:
            entities["monthly_shipments"] = monthly_shipments
            confidence["monthly_shipments"] = 0.9

    if "aggregator" in lower or "using" in lower or "use " in lower or normalize_shipkia_aggregator(value):
        aggregator_name = _shipkia_parse_aggregator_name(value) or normalize_shipkia_aggregator(value)
        if aggregator_name:
            entities["current_aggregator_status"] = "Yes"
            entities["current_aggregator_name"] = aggregator_name
            confidence["current_aggregator_status"] = 0.9
            confidence["current_aggregator_name"] = 0.9
        elif re.search(r"\b(no|nahi|nahin|not using|koi nahi|none|self|khud)\b", lower):
            entities["current_aggregator_status"] = "No"
            confidence["current_aggregator_status"] = 0.9

    rate_value = _shipkia_parse_current_shipping_rate(value, expected=True)
    if rate_value is not None and re.search(r"\b(rate|charges?|rs|rupees|inr)\b", lower):
        entities["current_shipping_rate"] = rate_value
        confidence["current_shipping_rate"] = 0.85
    rto_percentage = _shipkia_parse_rto_percentage(value, expected=True)
    if rto_percentage is not None:
        entities["rto_percentage"] = rto_percentage
        confidence["rto_percentage"] = 0.9

    rate_details = _extract_shipkia_rate_details(value)
    rate_entities = {
        key: rate_details.get(key)
        for key in SHIPKIA_RATE_SLOTS
        if rate_details.get(key) not in (None, "")
    }
    if rate_entities:
        entities["rate_details"] = rate_entities
        for key in rate_entities:
            confidence[f"rate_details.{key}"] = 0.85

    return entities, confidence


def _shipkia_router_entities_from_text(text: str, details: dict[str, Any], latest_prompt: str) -> tuple[dict[str, Any], dict[str, float]]:
    entities: dict[str, Any] = {}
    confidence: dict[str, float] = {}
    profile_language = bool(
        re.search(
            r"\b(business|store|brand|company|shop|monthly|mahine|shipments?|orders?)\b",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )
    profile = _shipkia_extract_compact_profile(
        text,
        expected=profile_language
        or latest_prompt
        in {
            "business_type",
            "compact_profile",
            "business_name",
            "business_name_only",
            "profile_business_monthly",
            "profile_business_aggregator",
            "profile_monthly_aggregator",
            "current_aggregator_status",
            "current_aggregator_status_only",
            "current_aggregator_name",
            "monthly_shipments",
        },
    )
    for key, value in profile.items():
        if value in (None, ""):
            continue
        entities[key] = value
        confidence[key] = 0.85 if latest_prompt else 0.7

    has_explicit_business_label = bool(
        re.search(
            r"\b(?:my\s+)?(?:brand|business|store|company|shop)\s*(?:name)?\s*(?:is|hai|=|:)\s+|\b(?:brand|business|store|company|shop)\s+name\s+",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )
    explicit_business_name = _shipkia_extract_business_name(text) if has_explicit_business_label else ""
    if explicit_business_name and entities.get("business_name") in (None, ""):
        entities["business_name"] = explicit_business_name
        confidence["business_name"] = 0.85
    elif entities.get("business_name") in (None, ""):
        possible_business_name = _shipkia_possible_business_name(text)
        if possible_business_name:
            entities["business_name"] = possible_business_name
            confidence["business_name"] = 0.6

    if details.get("rate_intent") and "current_shipping_rate" in entities:
        entities.pop("current_shipping_rate", None)
        confidence.pop("current_shipping_rate", None)

    if latest_prompt in {"current_rate_rto", "current_shipping_rate", "rto_percentage"}:
        rate_value = _shipkia_parse_current_shipping_rate(text, expected=True)
        if rate_value is not None:
            entities["current_shipping_rate"] = rate_value
            confidence["current_shipping_rate"] = 0.85
        rto_value = _shipkia_parse_rto_percentage(text, expected=True)
        if rto_value is not None:
            entities["rto_percentage"] = rto_value
            confidence["rto_percentage"] = 0.9

    rate_entities = {
        key: details.get(key)
        for key in SHIPKIA_RATE_SLOTS
        if details.get(key) not in (None, "")
    }
    if rate_entities:
        entities["rate_details"] = rate_entities
        for key in rate_entities:
            confidence[f"rate_details.{key}"] = 0.85 if details.get("rate_intent") or latest_prompt in SHIPKIA_RATE_PROMPT_MARKERS else 0.7

    return entities, confidence


def _shipkia_has_confident_lead_entities(
    entities: dict[str, Any],
    field_confidence: dict[str, float],
    min_confidence: float = 0.8,
) -> bool:
    for key in SHIPKIA_LEAD_CONTEXT_SLOTS:
        if entities.get(key) not in (None, "") and float(field_confidence.get(key) or 0.0) >= min_confidence:
            return True
    return False


def _shipkia_raise_compact_profile_confidence(
    entities: dict[str, Any],
    field_confidence: dict[str, float],
) -> None:
    profile_keys = {
        "business_name",
        "monthly_shipments",
        "current_aggregator_status",
        "current_aggregator_name",
    }
    present = [key for key in profile_keys if entities.get(key) not in (None, "")]
    if len(present) < 2:
        return
    for key in present:
        field_confidence[key] = max(float(field_confidence.get(key) or 0.0), 0.85)


def _shipkia_route_user_message(
    text: str,
    conversation: str | None,
    saved_context: dict[str, Any],
    chat_context: dict[str, Any],
    details: dict[str, Any],
) -> dict[str, Any]:
    latest_prompt = str((chat_context or {}).get("latest_prompt") or "")
    intent = "unclear"
    confidence = 0.45
    entities, field_confidence = _shipkia_router_entities_from_text(text, details, latest_prompt)
    _shipkia_raise_compact_profile_confidence(entities, field_confidence)
    correction_entities: dict[str, Any] = {}
    correction_confidence: dict[str, float] = {}

    if _shipkia_correction_intent(text):
        correction_entities, correction_confidence = _shipkia_extract_correction_entities(text)
        entities.update(correction_entities)
        field_confidence.update(correction_confidence)
        intent = "correction_update" if correction_entities else "correction_unclear"
        confidence = 0.9 if correction_entities else 0.65
    elif _shipkia_is_greeting(text) and not details.get("rate_intent"):
        intent = "greeting"
        confidence = 0.95
    elif _shipkia_customer_memory_intent(text):
        intent = "saved_info_question"
        confidence = 0.92
    elif _shipkia_price_objection_intent(text) or _shipkia_negotiation_intent(text):
        intent = "price_objection"
        confidence = 0.92
    elif _shipkia_frustration_intent(text):
        intent = "frustration"
        confidence = 0.9
    elif _shipkia_exact_rate_intent(text):
        intent = "exact_rate_request"
        confidence = 0.9
    elif _shipkia_onboarding_intent(text):
        intent = "onboarding_request"
        confidence = 0.88
    elif _shipkia_rate_card_request(text):
        intent = "rate_card_request"
        confidence = 0.88
    elif _shipkia_reset_intent(text):
        intent = "reset"
        confidence = 0.95
    elif details.get("rate_intent"):
        intent = "rate_request"
        confidence = 0.88
    elif latest_prompt and _shipkia_text_answers_prompt(text, latest_prompt, details):
        intent = "answer_to_prompt"
        confidence = 0.85
    elif _shipkia_text_has_customer_update(text, details):
        intent = "qualification_update"
        confidence = 0.78
    elif _shipkia_feature_question_intent(text):
        intent = "general_shipkia_question"
        confidence = 0.86

    has_confident_lead_entities = _shipkia_has_confident_lead_entities(entities, field_confidence)
    should_save = bool(
        (intent in {"answer_to_prompt", "correction_update"} and entities)
        or (intent == "qualification_update" and has_confident_lead_entities)
        or has_confident_lead_entities
    )
    should_calculate_rate = intent == "rate_request" or latest_prompt in SHIPKIA_RATE_PROMPT_MARKERS
    decision = {
        "intent": intent,
        "confidence": confidence,
        "entities": entities,
        "field_confidence": field_confidence,
        "should_save": should_save,
        "should_calculate_rate": should_calculate_rate,
        "latest_prompt": latest_prompt,
        "has_saved_context": bool(saved_context),
        "active_workflow": bool((chat_context or {}).get("active")),
    }
    _shipkia_log_router_decision(conversation, text, decision)
    return decision


def _shipkia_log_router_decision(conversation: str | None, text: str, decision: dict[str, Any]) -> None:
    try:
        _log_ai_timing(
            "shipkia_router",
            conversation=conversation,
            intent=decision.get("intent"),
            confidence=decision.get("confidence"),
            latest_prompt=decision.get("latest_prompt"),
            should_save=1 if decision.get("should_save") else 0,
            should_calculate_rate=1 if decision.get("should_calculate_rate") else 0,
            entity_keys=",".join(sorted(str(key) for key in (decision.get("entities") or {}).keys())),
            text_preview=str(text or "")[:160],
        )
    except Exception:
        pass


def _shipkia_apply_router_entities(context: dict[str, Any], decision: dict[str, Any], min_confidence: float = 0.8) -> dict[str, Any]:
    applied: dict[str, Any] = {}
    entities = decision.get("entities") or {}
    confidence = decision.get("field_confidence") or {}
    for key, value in entities.items():
        if key == "rate_details":
            rate_updates = {}
            for rate_key, rate_value in (value or {}).items():
                if rate_value in (None, ""):
                    continue
                if float(confidence.get(f"rate_details.{rate_key}", 0.0) or 0.0) < min_confidence:
                    continue
                context.setdefault("rate_details", {})[rate_key] = rate_value
                rate_updates[rate_key] = rate_value
            if rate_updates:
                applied["rate_details"] = rate_updates
            continue
        if value in (None, ""):
            continue
        if float(confidence.get(key, 0.0) or 0.0) < min_confidence:
            continue
        context[key] = value
        applied[key] = value
    _shipkia_clear_aggregator_dependent_context(context)
    return applied


def _shipkia_correction_saved_reply(applied: dict[str, Any]) -> str:
    labels = []
    for key in applied:
        if key == "business_name":
            labels.append("business/store name")
        elif key == "monthly_shipments":
            labels.append("monthly shipments")
        elif key == "current_aggregator_name":
            labels.append("current aggregator")
        elif key == "current_aggregator_status":
            labels.append("aggregator status")
        elif key == "current_shipping_rate":
            labels.append("current shipping rate")
        elif key == "rto_percentage":
            labels.append("RTO %")
        elif key == "rate_details":
            labels.append("shipping/rate details")
    if not labels:
        return "Mujhe correction clear nahi hua. Aap ek baar detail clearly share kar dijiye."
    return "Done, maine " + ", ".join(dict.fromkeys(labels)) + " update kar diya."


def _shipkia_low_confidence_update_reply(decision: dict[str, Any]) -> str:
    entities = decision.get("entities") or {}
    parts = []
    if entities.get("business_name"):
        parts.append(f"business/store name {entities.get('business_name')}")
    if entities.get("monthly_shipments") not in (None, ""):
        parts.append(f"monthly shipments {_shipkia_format_value(entities.get('monthly_shipments'))}")
    if entities.get("current_aggregator_name"):
        parts.append(f"current aggregator {entities.get('current_aggregator_name')}")
    if parts:
        return "Mujhe yeh details samajh aayi: " + ", ".join(parts) + ". Please ek baar clearly confirm kar dijiye."
    return "Mujhe detail thodi unclear lagi. Aap business/store name, monthly shipments aur current aggregator ek message mein clearly share kar dijiye."


def _shipkia_single_location_answer(text: str) -> dict[str, Any]:
    body = (text or "").strip()
    if not body or len(body) > 60:
        return {}
    pins = re.findall(r"(?<!\d)([1-9]\d{5})(?!\d)", body)
    if len(pins) == 1:
        return {"pincode": pins[0]}
    if re.search(r"\b(rate|rates|price|charge|charges|kg|gram|prepaid|cod|zone|orders?|shipments?)\b", body, flags=re.IGNORECASE):
        return {}
    city = _normalise_rate_city(body)
    if city and _looks_like_rate_city(city):
        return {"city": city}
    return {}


def _extract_shipkia_rate_details(text: str) -> dict[str, Any]:
    body = (text or "").strip()
    body = re.sub(r"^\s*(?:bataya|btaya|bataaya)\s+to\s+", "", body, flags=re.IGNORECASE)
    lower = body.lower()
    if _shipkia_looks_like_current_rate_answer(body):
        return {"rate_intent": False}
    explicit_rate_intent = bool(
        re.search(
            r"\b(rate|rates|price|pricing|charge|charges|freight|shipping cost|courier cost|flat rate|flat rates)\b",
            lower,
        )
        or "kitna" in lower
    )
    details: dict[str, Any] = {"rate_intent": explicit_rate_intent}

    weight_match = re.search(
        r"\b(\d+(?:\.\d+)?)\s*(kg|kgs|kilogram|kilograms|kilo|kilos|kh|k\s*h|g|gr|gm|gms|grm|grms|gram|grams)\b",
        lower,
    )
    if weight_match:
        value = float(weight_match.group(1))
        unit = re.sub(r"\s+", "", weight_match.group(2))
        if unit.startswith("kg") or unit.startswith("kilo") or unit == "kh":
            details["weight_grams"] = round(value * 1000)
        else:
            details["weight_grams"] = round(value)

    if re.search(r"\bboth\b|\bdono\b|\bdonon\b|pre\s*paid.*\bcod\b|\bcod\b.*pre\s*paid", lower):
        details["payment_type"] = "Both"
    elif re.search(r"\bpre\s*paid\b|\bprepaid\b", lower):
        details["payment_type"] = "Prepaid"
    elif re.search(r"\bcod\b|cash\s*on\s*delivery", lower):
        details["payment_type"] = "COD"

    order_value_match = re.search(
        r"(?:order\s*value|cod\s*value|value|amount|rs\.?|inr|₹)\s*(?:is|hai|of|:)?\s*₹?\s*(\d+(?:,\d{3})*(?:\.\d+)?)",
        lower,
    )
    if order_value_match:
        details["order_value"] = float(order_value_match.group(1).replace(",", ""))
        if explicit_rate_intent:
            details["rate_intent"] = True

    delivery_scope = _shipkia_pan_india_scope(body)
    if delivery_scope:
        details["delivery_scope"] = delivery_scope
        details["rate_intent"] = True
        pickup_city = _shipkia_pickup_city_for_pan_india(body)
        if pickup_city:
            details["pickup_city"] = pickup_city

    route_text = _strip_shipkia_rate_noise(body)
    route_end = (
        r"(?=\s*(?:,|\.|$|\d+(?:\.\d+)?\s*"
        r"(?:kg|kgs|kilogram|kilograms|kilo|kilos|g|gr|gm|gms|grm|grms|gram|grams)\b|"
        r"\b(?:pre\s*paid|prepaid|cod|both|dono|order\s*value|cod\s*value|value|amount|rs|inr)\b))"
    )
    route_patterns = (
        rf"\bfrom\s+([a-zA-Z][a-zA-Z\s]{{1,40}}?)\s+(?:to|se|->)\s+([a-zA-Z][a-zA-Z\s]{{1,40}}?){route_end}",
        rf"\b([a-zA-Z][a-zA-Z\s]{{1,40}}?)\s+(?:to|se|->)\s+([a-zA-Z][a-zA-Z\s]{{1,40}}?){route_end}",
    )
    for pattern in route_patterns:
        match = re.search(pattern, route_text, flags=re.IGNORECASE)
        if not match:
            continue
        pickup = re.sub(
            r"\b(rate|rates|price|pricing|tell|me|mujhe|bata|bta|btao|btana|batao|dijiye|dijea|dije|dijie|chahiye|chaiye|yes|haan|ha|ji|do|for|from)\b",
            " ",
            match.group(1),
            flags=re.IGNORECASE,
        )
        delivery = re.sub(
            r"\b(rate|rates|price|pricing|tell|me|mujhe|bata|bta|btao|btana|batao|dijiye|dijea|dije|dijie|chahiye|chaiye|yes|haan|ha|ji|do|for|from)\b",
            " ",
            match.group(2),
            flags=re.IGNORECASE,
        )
        pickup = _normalise_rate_city(pickup)
        delivery = _normalise_rate_city(delivery)
        if pickup and delivery and _looks_like_rate_city(pickup) and _looks_like_rate_city(delivery):
            details["pickup_city"] = pickup
            details["delivery_city"] = delivery
            details["rate_intent"] = True
            break

    pins = re.findall(r"(?<!\d)([1-9]\d{5})(?!\d)", body)
    if pins:
        details["pickup_pincode"] = pins[0]
        details["rate_intent"] = True
    if len(pins) > 1:
        details["delivery_pincode"] = pins[1]

    if "flat" in lower:
        details["rate_type"] = "flat_rate"
    zone_match = re.search(r"\b(?:zone\s*)?([a-f])\s*zone\b|\bzone\s*([a-f])\b", lower)
    if zone_match:
        details["zone"] = (zone_match.group(1) or zone_match.group(2) or "").upper()

    return details


def _shipkia_completed_rate_details(details: dict[str, Any] | None) -> bool:
    details = details or {}
    has_pickup = bool(details.get("pickup_city") or details.get("pickup_pincode"))
    has_delivery = bool(details.get("delivery_city") or details.get("delivery_pincode") or details.get("delivery_scope"))
    return bool(has_pickup and has_delivery and details.get("weight_grams") and details.get("payment_type"))


def _shipkia_slot_only_rate_answer(details: dict[str, Any] | None) -> bool:
    details = details or {}
    if details.get("rate_intent"):
        return False
    return bool(details.get("weight_grams") or details.get("payment_type") or details.get("order_value")) and not any(
        details.get(key)
        for key in (
            "pickup_city",
            "delivery_city",
            "pickup_pincode",
            "delivery_pincode",
            "delivery_scope",
            "zone",
            "rate_type",
        )
    )


def _shipkia_rate_followup(
    details: dict[str, Any],
    conversation: str | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    has_pickup = bool(details.get("pickup_city") or details.get("pickup_pincode"))
    has_delivery = bool(
        details.get("delivery_city")
        or details.get("delivery_pincode")
        or details.get("delivery_scope")
    )
    if not has_pickup or not has_delivery or not details.get("weight_grams"):
        if has_pickup and has_delivery and not details.get("weight_grams"):
            _shipkia_save_conversation_state(conversation, "weight_grams", context or {})
            return _shipkia_render_reply_template("weight_only", conversation, context or {})
        if has_pickup and not has_delivery:
            _shipkia_save_conversation_state(conversation, "pickup_delivery", context or {})
            return _shipkia_render_reply_template("pickup_delivery", conversation, context or {})
        _shipkia_save_conversation_state(conversation, "weight_grams", context or {})
        return _shipkia_render_reply_template("weight_grams", conversation, context or {})
    if details.get("payment_type") not in {"Prepaid", "COD", "Both"}:
        _shipkia_save_conversation_state(conversation, "payment_type", context or {})
        return _shipkia_render_reply_template("payment_type", conversation, context or {})
    return ""


def _shipkia_rate_details_have_customer_slot(details: dict[str, Any]) -> bool:
    return any(
        details.get(field)
        for field in (
            "pickup_city",
            "delivery_city",
            "pickup_pincode",
            "delivery_pincode",
            "weight_grams",
            "delivery_scope",
            "zone",
            "rate_type",
        )
    )


def _shipkia_prompt_kind(text: str) -> str:
    lower = (text or "").casefold()
    for kind, markers in {**SHIPKIA_WORKFLOW_PROMPT_MARKERS, **SHIPKIA_RATE_PROMPT_MARKERS}.items():
        if any(marker in lower for marker in markers):
            return kind
    return ""


def _shipkia_parse_business_type(text: str) -> str:
    lower = (text or "").casefold()
    if re.search(r"\bb\s*2\s*c\b|\bb2c\b", lower):
        return "B2C"
    if re.search(r"\bd\s*2\s*c\b|\bd2c\b", lower):
        return "D2C"
    return ""


def _shipkia_negative_aggregator_answer(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(
            r"\b(not\s+using|not\s+use|don't\s+use|do\s+not\s+use|no|nahi|nahin|nhi|na|koi\s+nahi|none|self|khud)\b"
            r".*\b(aggregator|logistics?|shipping partner|courier partner)\b",
            lower,
        )
        or re.search(
            r"\b(aggregator|logistics?|shipping partner|courier partner)\b"
            r".*\b(no|nahi|nahin|nhi|na|koi\s+nahi|none|use nahi|using nahi)\b",
            lower,
        )
    )


def _shipkia_refuses_requested_info(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    if not lower:
        return False
    return bool(
        re.search(
            r"\b(nahi|nahin|nhi|na|no|not|don't|do not)\b.*\b(bataunga|btaunga|batana|btaana|tell|share|provide|dunga|dungi|dena)\b",
            lower,
        )
        or re.search(
            r"\b(don't want|do not want|dont want|nahi dena|nhi dena|nahi batana|nhi batana|kuch bhi nahi|kch bhe|kch bhi)\b",
            lower,
        )
    )


def _shipkia_parse_yes_no(text: str) -> str:
    lower = (text or "").casefold().strip()
    if re.search(r"\b(no|nahi|nahin|nhi|na|not using|not use|don't use|do not use|koi nahi|none)\b", lower):
        return "No"
    if re.search(r"\b(yes|haan|ha|hanji|haji|ji|use|using|karte|kar rahe)\b", lower):
        return "Yes"
    return ""


def _shipkia_parse_monthly_shipments(text: str, expected: bool = False) -> int | None:
    lower = (text or "").casefold()
    if not expected and not re.search(r"\b(month|monthly|mahine|shipment|shipments|order|orders)\b", lower):
        return None
    match = re.search(r"\b(\d+(?:,\d{3})*(?:\.\d+)?)(?:\s*(k|thousand))?\b", lower)
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    value = int(round(amount))
    if match.group(2):
        value = int(round(amount * 1000))
    return value


def _shipkia_parse_number_answer(text: str) -> float | None:
    lower = (text or "").casefold()
    for match in re.finditer(r"₹?\s*(\d+(?:,\d{3})*(?:\.\d+)?)\s*(?:rs\.?|rupees|inr)?", lower):
        suffix = lower[match.end() : match.end() + 8]
        if re.match(r"\s*(?:kg|kgs|kilogram|kilograms|g|gm|gms|gram|grams)\b", suffix):
            continue
        try:
            return float(match.group(1).replace(",", ""))
        except ValueError:
            continue
    return None


def _shipkia_numeric_tokens(text: str) -> list[dict[str, Any]]:
    lower = (text or "").casefold()
    tokens: list[dict[str, Any]] = []
    for match in re.finditer(r"(?<!\d)(\d+(?:,\d{3})*(?:\.\d+)?)(?:\s*(%|rs\.?|rupees|inr|kg|kgs|g|gm|gms|grm|grms|grams?))?", lower):
        suffix = (match.group(2) or "").strip()
        if suffix in {"kg", "kgs", "g", "gm", "gms", "grm", "grms", "gram", "grams"}:
            continue
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        tokens.append(
            {
                "value": value,
                "suffix": suffix,
                "before": lower[max(0, match.start() - 24) : match.start()],
                "after": lower[match.end() : match.end() + 24],
            }
        )
    return tokens


def _shipkia_parse_rate_rto_pair(text: str) -> tuple[float | None, float | None]:
    lower = (text or "").casefold()
    rate_value: float | None = None
    rto_value: float | None = None

    rto_match = re.search(r"\b(?:rto|return|returns)[^\d]{0,25}(\d+(?:\.\d+)?)\s*%?", lower)
    if rto_match:
        rto_value = float(rto_match.group(1))

    rate_patterns = (
        r"\b(?:current\s*)?(?:shipping\s*)?(?:rate|charges?|freight)[^\d]{0,25}(?:rs\.?|inr|₹)?\s*(\d+(?:\.\d+)?)",
        r"(?:rs\.?|inr|₹)\s*(\d+(?:\.\d+)?)",
        r"\b(\d+(?:\.\d+)?)\s*(?:rs\.?|rupees|inr)\b",
    )
    for pattern in rate_patterns:
        match = re.search(pattern, lower)
        if match:
            rate_value = float(match.group(1))
            break

    tokens = _shipkia_numeric_tokens(text)
    if rto_value is None:
        for token in tokens:
            if token["suffix"] == "%" or re.search(r"\b(rto|return|returns)\b", token["before"]):
                rto_value = token["value"]
                break
    if rate_value is None:
        for token in tokens:
            if token["value"] == rto_value and token["suffix"] == "%":
                continue
            if re.search(r"\b(rto|return|returns)\b", token["before"]):
                continue
            rate_value = token["value"]
            break

    if len(tokens) >= 2 and (rate_value is None or rto_value is None):
        percent_indexes = [idx for idx, token in enumerate(tokens) if token["suffix"] == "%"]
        if percent_indexes:
            percent_index = percent_indexes[0]
            if rto_value is None:
                rto_value = tokens[percent_index]["value"]
            if rate_value is None:
                for idx, token in enumerate(tokens):
                    if idx != percent_index:
                        rate_value = token["value"]
                        break
        else:
            if rate_value is None:
                rate_value = tokens[0]["value"]
            if rto_value is None:
                rto_value = tokens[1]["value"]

    if rto_value is not None and not 0 <= rto_value <= 100:
        rto_value = None
    if rate_value is not None and rate_value <= 0:
        rate_value = None
    return rate_value, rto_value


def _shipkia_parse_current_shipping_rate(text: str, expected: bool = False) -> float | None:
    lower = (text or "").casefold()
    if not expected and not re.search(r"(₹|\brs\.?\b|rupees|inr|\brate\b|\brates\b|charge|charges)", lower):
        return None
    if expected:
        rate_value, _rto_value = _shipkia_parse_rate_rto_pair(text)
        if rate_value is not None:
            return rate_value
        if re.search(r"\d+(?:\.\d+)?\s*%", lower):
            return None
    value = _shipkia_parse_number_answer(text)
    if value is None or value <= 0:
        return None
    return value


def _shipkia_parse_rto_percentage(text: str, expected: bool = False) -> float | None:
    lower = (text or "").casefold()
    if not expected and not re.search(r"\b(rto|return|returns|percentage|percent|%)\b", lower):
        return None
    if expected:
        _rate_value, rto_value = _shipkia_parse_rate_rto_pair(text)
        if rto_value is not None:
            return rto_value
    percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%", lower)
    if percent_match:
        value = float(percent_match.group(1))
    else:
        rto_match = re.search(r"\b(?:rto|return|returns)[^\d]{0,25}(\d+(?:\.\d+)?)", lower)
        if rto_match:
            value = float(rto_match.group(1))
        elif expected:
            return None
        else:
            value = _shipkia_parse_number_answer(text)
    if value is None or value < 0 or value > 100:
        return None
    return value


def _shipkia_safe_free_text(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    if not value or len(value) > 120:
        return ""
    if value.casefold() in {"hi", "hello", "hey", "hii"}:
        return ""
    if _shipkia_non_business_free_text(value):
        return ""
    return value


def _shipkia_is_greeting(text: str) -> bool:
    normalised = re.sub(r"[^a-z]+", "", str(text or "").casefold())
    return normalised in {"hi", "hii", "hiii", "hello", "helo", "hlo", "hey"}


def _shipkia_reset_intent(text: str) -> bool:
    lower = re.sub(r"\s+", " ", str(text or "").casefold()).strip()
    return bool(re.search(r"\b(start over|restart|fresh start|start fresh|new query|naya start|dobara start)\b", lower))


def _shipkia_context_is_complete(context: dict[str, Any]) -> bool:
    required = (
        "business_type",
        "business_name",
        "current_aggregator_status",
        "monthly_shipments",
    )
    if any(context.get(field) in (None, "") for field in required):
        return False
    if not _shipkia_has_monthly_shipments(context):
        return False
    if context.get("current_aggregator_status") == "Yes":
        if not context.get("current_aggregator_name"):
            return False
        if not _shipkia_has_positive_number(context.get("current_shipping_rate")):
            return False
        if not _shipkia_has_positive_number(context.get("rto_percentage")):
            return False
    return True


def _shipkia_has_positive_number(value: Any) -> bool:
    if value in (None, "", 0, 0.0, "0", "0.0"):
        return False
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _shipkia_has_monthly_shipments(context: dict[str, Any]) -> bool:
    value = context.get("monthly_shipments")
    if value in (None, "", 0, "0"):
        return False
    return True


def _shipkia_phone_variants(phone: str) -> list[str]:
    raw = str(phone or "").strip()
    digits = normalize_phone(raw)
    variants = [raw, digits]
    if digits.startswith("91") and len(digits) == 12:
        variants.extend([digits[-10:], f"+91{digits[-10:]}"])
    elif len(digits) == 10:
        variants.extend([f"91{digits}", f"+91{digits}"])
    return [value for value in dict.fromkeys(variants) if value]


def _shipkia_find_lead_by_phone(phone: str) -> str:
    if not phone or not safe_ai_exists("DocType", SHIPKIA_LEAD_DOCTYPE):
        return ""
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    variants = _shipkia_phone_variants(phone)
    for fieldname in ("mobile_no", "whatsapp_no", "phone", "custom_whatsapp_number"):
        if not meta.has_field(fieldname):
            continue
        for value in variants:
            lead = safe_ai_get_value(SHIPKIA_LEAD_DOCTYPE, {fieldname: value}, "name")
            if lead:
                return str(lead)

        digits = normalize_phone(phone)
        last10 = digits[-10:] if len(digits) >= 10 else digits
        if not last10:
            continue
        rows = safe_ai_get_all(
            SHIPKIA_LEAD_DOCTYPE,
            filters={fieldname: ["like", f"%{last10}%"]},
            fields=["name", fieldname],
            limit_page_length=20,
        )
        for row in rows:
            value = normalize_phone(row.get(fieldname))
            if value and (value == digits or value.endswith(last10)):
                return str(row.get("name") or "")
    return ""


def _shipkia_conversation_contact(conversation: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    if not conversation:
        return {}, {}
    convo = safe_ai_get_value(
        "Chat Conversation",
        conversation,
        ["name", "contact", "linked_reference_doctype", "linked_reference_name"],
        as_dict=True,
    ) or {}
    contact = {}
    if convo.get("contact"):
        contact = safe_ai_get_value(
            "Chat Contact",
            convo.get("contact"),
            ["name", "display_name", "phone_number", "linked_lead", "source_doctype", "source_name"],
            as_dict=True,
        ) or {}
    return dict(convo), dict(contact)


def _shipkia_create_lead_for_conversation(
    conversation: str | None,
    contact: dict[str, Any],
) -> str:
    if not safe_ai_exists("DocType", SHIPKIA_LEAD_DOCTYPE):
        return ""
    phone = str(contact.get("phone_number") or "").strip()
    if not phone:
        return ""

    display_name = str(contact.get("display_name") or "").strip()
    digits = normalize_phone(phone)
    fallback_name = digits[-10:] if len(digits) >= 10 else phone
    first_name = display_name if display_name and display_name != phone else fallback_name

    lead_meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    doc = frappe.get_doc(
        {
            "doctype": SHIPKIA_LEAD_DOCTYPE,
            "first_name": first_name,
            "lead_name": first_name,
            "mobile_no": phone,
        }
    )
    if lead_meta.has_field("status"):
        doc.status = "Open"
    if lead_meta.has_field("whatsapp_no"):
        doc.whatsapp_no = phone
    if lead_meta.has_field("shipkia_lead_source"):
        doc.shipkia_lead_source = "WhatsApp Inbound"
    if lead_meta.has_field("shipkia_first_contact_channel"):
        doc.shipkia_first_contact_channel = "WhatsApp"
    safe_ai_insert(doc)

    if contact.get("name"):
        safe_ai_set_value(
            "Chat Contact",
            contact.get("name"),
            {
                "linked_lead": doc.name,
                "source_doctype": SHIPKIA_LEAD_DOCTYPE,
                "source_name": doc.name,
            },
            update_modified=False,
        )
    if conversation:
        safe_ai_set_value(
            "Chat Conversation",
            conversation,
            {
                "linked_reference_doctype": SHIPKIA_LEAD_DOCTYPE,
                "linked_reference_name": doc.name,
            },
            update_modified=False,
        )
    return str(doc.name)


def _shipkia_resolve_lead(
    conversation: str | None,
    *,
    create_if_missing: bool = False,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    convo, contact = _shipkia_conversation_contact(conversation)
    lead_name = ""
    if convo.get("linked_reference_doctype") == SHIPKIA_LEAD_DOCTYPE and convo.get("linked_reference_name"):
        candidate = str(convo.get("linked_reference_name") or "")
        if safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, candidate):
            lead_name = candidate
    if not lead_name and contact.get("linked_lead") and safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, contact.get("linked_lead")):
        lead_name = str(contact.get("linked_lead"))
    if not lead_name and contact.get("source_doctype") == SHIPKIA_LEAD_DOCTYPE and contact.get("source_name"):
        candidate = str(contact.get("source_name") or "")
        if safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, candidate):
            lead_name = candidate
    if not lead_name and contact.get("phone_number"):
        lead_name = _shipkia_find_lead_by_phone(str(contact.get("phone_number") or ""))
    if not lead_name and create_if_missing:
        lead_name = _shipkia_create_lead_for_conversation(conversation, contact)
    return lead_name, convo, contact


def _shipkia_load_lead_context(lead_name: str) -> dict[str, Any]:
    if not lead_name or not safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, lead_name):
        return {}
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    fieldnames = [
        "lead_name",
        "first_name",
        "shipkia_business_type",
        "shipkia_business_name",
        "shipkia_order_source",
        "shipkia_current_aggregator_status",
        "shipkia_current_aggregator_name",
        "shipkia_current_shipping_rate",
        "shipkia_rto_percentage",
        "shipkia_monthly_shipments",
        "shipkia_pickup_city",
        "shipkia_delivery_city",
        "shipkia_delivery_scope",
        "shipkia_pickup_pincode",
        "shipkia_average_weight",
        "shipkia_payment_mode",
        "shipkia_context_completed",
    ]
    fieldnames = [fieldname for fieldname in fieldnames if meta.has_field(fieldname)]
    values = safe_ai_get_value(SHIPKIA_LEAD_DOCTYPE, lead_name, fieldnames, as_dict=True) or {}
    rate_details: dict[str, Any] = {
        "pickup_city": values.get("shipkia_pickup_city"),
        "delivery_city": values.get("shipkia_delivery_city"),
        "delivery_scope": values.get("shipkia_delivery_scope"),
        "pickup_pincode": values.get("shipkia_pickup_pincode"),
        "weight_grams": _shipkia_weight_grams_from_value(values.get("shipkia_average_weight")),
        "payment_type": values.get("shipkia_payment_mode"),
    }
    rate_details = {key: value for key, value in rate_details.items() if value not in (None, "")}
    context: dict[str, Any] = {
        "lead_name": lead_name,
        "customer_name": values.get("lead_name") or values.get("first_name"),
        "business_type": values.get("shipkia_business_type"),
        "business_name": values.get("shipkia_business_name"),
        "business_operation_mode": values.get("shipkia_order_source"),
        "current_aggregator_status": values.get("shipkia_current_aggregator_status"),
        "current_aggregator_name": values.get("shipkia_current_aggregator_name"),
        "current_shipping_rate": (
            values.get("shipkia_current_shipping_rate")
            if _shipkia_has_positive_number(values.get("shipkia_current_shipping_rate"))
            else None
        ),
        "rto_percentage": (
            values.get("shipkia_rto_percentage")
            if _shipkia_has_positive_number(values.get("shipkia_rto_percentage"))
            else None
        ),
        "monthly_shipments": (
            values.get("shipkia_monthly_shipments")
            if values.get("shipkia_monthly_shipments") not in (None, "", 0, "0")
            else None
        ),
        "context_completed": bool(values.get("shipkia_context_completed")),
        "rate_details": rate_details,
    }
    return {key: value for key, value in context.items() if value not in (None, "")}


def _shipkia_merge_saved_context(
    saved_context: dict[str, Any],
    chat_context: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(saved_context or {})
    for key, value in (chat_context or {}).items():
        if key == "rate_details":
            continue
        if value not in (None, ""):
            merged[key] = value
    merged["active"] = bool((chat_context or {}).get("active"))
    merged["latest_prompt"] = (chat_context or {}).get("latest_prompt") or ""
    merged_rate_details = dict((saved_context or {}).get("rate_details") or {})
    merged_rate_details.update(dict((chat_context or {}).get("rate_details") or {}))
    merged["rate_details"] = merged_rate_details
    _shipkia_clear_aggregator_dependent_context(merged)
    return merged


def _shipkia_weight_grams_from_value(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return round(float(value))
    text = str(value).strip().lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*(kg|kgs|kilogram|kilograms|g|gm|gms|gram|grams)?", text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2) or "g"
    if unit.startswith("kg") or unit.startswith("kilo"):
        return round(amount * 1000)
    return round(amount)


def _shipkia_format_weight_grams(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        grams = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if grams <= 0:
        return ""
    if grams >= 1000 and grams % 1000 == 0:
        return f"{int(grams / 1000)}kg"
    if grams >= 1000:
        return f"{grams / 1000:g}kg"
    return f"{int(grams)}g" if grams.is_integer() else f"{grams:g}g"


def _shipkia_save_rate_details_to_lead(lead_name: str, rate_details: dict[str, Any]) -> None:
    if not lead_name or not rate_details or not safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, lead_name):
        return
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    updates: dict[str, Any] = {}

    field_map = {
        "pickup_city": "shipkia_pickup_city",
        "delivery_city": "shipkia_delivery_city",
        "delivery_scope": "shipkia_delivery_scope",
        "payment_type": "shipkia_payment_mode",
    }
    for source, target in field_map.items():
        value = rate_details.get(source)
        if value not in (None, "") and meta.has_field(target):
            updates[target] = value

    if rate_details.get("weight_grams") and meta.has_field("shipkia_average_weight"):
        updates["shipkia_average_weight"] = _shipkia_format_weight_grams(rate_details.get("weight_grams"))

    if rate_details.get("pickup_pincode") and meta.has_field("shipkia_pickup_pincode"):
        pickup_pincode = str(rate_details.get("pickup_pincode") or "").strip()
        existing = safe_ai_get_value(SHIPKIA_LEAD_DOCTYPE, lead_name, "shipkia_pickup_pincode") or ""
        pins = [pin.strip() for pin in re.split(r"[\s,]+", str(existing)) if pin.strip()]
        if pickup_pincode and pickup_pincode not in pins:
            pins.append(pickup_pincode)
        updates["shipkia_pickup_pincode"] = "\n".join(pins)

    if updates and meta.has_field("shipkia_context_updated_at"):
        updates["shipkia_context_updated_at"] = now_datetime()
    if updates:
        safe_ai_set_value(SHIPKIA_LEAD_DOCTYPE, lead_name, updates, update_modified=True)


def _shipkia_save_lead_context(
    lead_name: str,
    context: dict[str, Any],
    field_confidence: dict[str, float] | None = None,
    only_confident: bool = False,
) -> None:
    if not lead_name or not safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, lead_name):
        return
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    field_map = {
        "business_type": "shipkia_business_type",
        "business_name": "shipkia_business_name",
        "business_operation_mode": "shipkia_order_source",
        "current_aggregator_status": "shipkia_current_aggregator_status",
        "current_aggregator_name": "shipkia_current_aggregator_name",
        "current_shipping_rate": "shipkia_current_shipping_rate",
        "rto_percentage": "shipkia_rto_percentage",
        "monthly_shipments": "shipkia_monthly_shipments",
    }
    updates: dict[str, Any] = {}
    for source, target in field_map.items():
        value = context.get(source)
        if value in (None, "") or not meta.has_field(target):
            continue
        if only_confident and source not in (field_confidence or {}):
            continue
        if source in (field_confidence or {}) and float((field_confidence or {}).get(source) or 0.0) < 0.8:
            continue
        existing_value = safe_ai_get_value(SHIPKIA_LEAD_DOCTYPE, lead_name, target)
        if (
            isinstance(value, str)
            and isinstance(existing_value, str)
            and existing_value.strip()
            and existing_value.casefold() == value.casefold()
        ):
            continue
        updates[target] = value

    if str(context.get("current_aggregator_status") or "").casefold() == "no":
        clear_values = {
            "shipkia_current_aggregator_name": "",
            "shipkia_current_shipping_rate": 0,
            "shipkia_rto_percentage": 0,
            "shipkia_current_aggregator_verified": 0,
            "shipkia_current_aggregator_raw": "",
        }
        for fieldname, value in clear_values.items():
            if meta.has_field(fieldname):
                updates[fieldname] = value

    aggregator_name = str(context.get("current_aggregator_name") or "").strip()
    can_update_aggregator_metadata = (not only_confident) or "current_aggregator_name" in (field_confidence or {})
    if str(context.get("current_aggregator_status") or "").casefold() != "no" and aggregator_name and can_update_aggregator_metadata:
        if meta.has_field("shipkia_current_aggregator_status"):
            updates["shipkia_current_aggregator_status"] = "Yes"
        verified_name = normalize_shipkia_aggregator(aggregator_name)
        if verified_name:
            if meta.has_field("shipkia_current_aggregator_name"):
                updates["shipkia_current_aggregator_name"] = verified_name
            if meta.has_field("shipkia_current_aggregator_verified"):
                updates["shipkia_current_aggregator_verified"] = 1
            if meta.has_field("shipkia_current_aggregator_raw"):
                updates["shipkia_current_aggregator_raw"] = ""
        else:
            if meta.has_field("shipkia_current_aggregator_verified"):
                updates["shipkia_current_aggregator_verified"] = 0
            if meta.has_field("shipkia_current_aggregator_raw"):
                updates["shipkia_current_aggregator_raw"] = aggregator_name

    if (not only_confident) and meta.has_field("shipkia_context_completed"):
        updates["shipkia_context_completed"] = 1 if _shipkia_context_is_complete(context) else 0
    if updates and meta.has_field("shipkia_context_updated_at"):
        updates["shipkia_context_updated_at"] = now_datetime()
    if updates:
        safe_ai_set_value(SHIPKIA_LEAD_DOCTYPE, lead_name, updates, update_modified=True)
    if not only_confident:
        _shipkia_save_rate_details_to_lead(lead_name, context.get("rate_details") or {})


def _shipkia_mark_rate_shared(lead_name: str) -> None:
    if not lead_name or not safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, lead_name):
        return
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    updates: dict[str, Any] = {}
    if meta.has_field("shipkia_rate_shared"):
        updates["shipkia_rate_shared"] = 1
    if meta.has_field("shipkia_ai_last_action"):
        updates["shipkia_ai_last_action"] = "Starting rate shared"
    if updates:
        safe_ai_set_value(SHIPKIA_LEAD_DOCTYPE, lead_name, updates, update_modified=True)


def _shipkia_mark_signup_link_sent(lead_name: str) -> None:
    if not lead_name or not safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, lead_name):
        return
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    updates: dict[str, Any] = {}
    if meta.has_field("shipkia_signup_link_sent"):
        updates["shipkia_signup_link_sent"] = 1
    if meta.has_field("shipkia_ai_onboarding_assisted"):
        updates["shipkia_ai_onboarding_assisted"] = 1
    if meta.has_field("shipkia_ai_onboarding_stage"):
        updates["shipkia_ai_onboarding_stage"] = "Signup Link Shared"
    if meta.has_field("shipkia_ai_last_action"):
        updates["shipkia_ai_last_action"] = "Signup link shared"
    if updates:
        safe_ai_set_value(SHIPKIA_LEAD_DOCTYPE, lead_name, updates, update_modified=True)


def _shipkia_mark_human_review_needed(lead_name: str, note: str) -> None:
    if not lead_name or not safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, lead_name):
        return
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    updates: dict[str, Any] = {}
    if meta.has_field("shipkia_callback_required"):
        updates["shipkia_callback_required"] = 1
    if meta.has_field("shipkia_ai_qualification_status"):
        updates["shipkia_ai_qualification_status"] = "Needs Human Review"
    if meta.has_field("shipkia_ai_lead_temperature"):
        updates["shipkia_ai_lead_temperature"] = "Hot"
    if meta.has_field("shipkia_lead_temperature"):
        updates["shipkia_lead_temperature"] = "Hot"
    if meta.has_field("shipkia_objections"):
        existing = safe_ai_get_value(SHIPKIA_LEAD_DOCTYPE, lead_name, "shipkia_objections") or ""
        updates["shipkia_objections"] = f"{existing}\n{note}".strip() if existing else note
    if meta.has_field("shipkia_followup_notes"):
        existing = safe_ai_get_value(SHIPKIA_LEAD_DOCTYPE, lead_name, "shipkia_followup_notes") or ""
        followup = "Human follow-up needed: customer showed frustration after qualification flow."
        updates["shipkia_followup_notes"] = f"{existing}\n{followup}".strip() if existing else followup
    if updates:
        safe_ai_set_value(SHIPKIA_LEAD_DOCTYPE, lead_name, updates, update_modified=True)


def _shipkia_save_callback_request(lead_name: str, callback_time: str) -> None:
    if not lead_name or not safe_ai_exists(SHIPKIA_LEAD_DOCTYPE, lead_name):
        return
    meta = frappe.get_meta(SHIPKIA_LEAD_DOCTYPE)
    updates: dict[str, Any] = {}
    if meta.has_field("shipkia_callback_required"):
        updates["shipkia_callback_required"] = 1
    if callback_time and meta.has_field("shipkia_followup_notes"):
        existing = safe_ai_get_value(SHIPKIA_LEAD_DOCTYPE, lead_name, "shipkia_followup_notes") or ""
        note = f"Callback requested time: {callback_time}"
        updates["shipkia_followup_notes"] = f"{existing}\n{note}".strip() if existing else note
    if callback_time and meta.has_field("shipkia_next_follow_up"):
        try:
            parsed = get_datetime(callback_time)
            if parsed:
                updates["shipkia_next_follow_up"] = parsed
        except Exception:
            pass
    if updates:
        safe_ai_set_value(SHIPKIA_LEAD_DOCTYPE, lead_name, updates, update_modified=True)


def _shipkia_greeting_reply(
    saved_context: dict[str, Any],
    contact: dict[str, Any],
    conversation: str | None = None,
) -> str:
    name = str(
        saved_context.get("customer_name")
        or contact.get("display_name")
        or saved_context.get("business_name")
        or ""
    ).strip()
    if name and normalize_phone(name) == normalize_phone(str(contact.get("phone_number") or "")):
        name = ""
    if name:
        return _shipkia_render_reply_template(
            "greeting_known",
            conversation,
            saved_context,
            customer_name=name,
            first_name=name.split()[0],
        )
    return _shipkia_render_reply_template("greeting_unknown", conversation, saved_context)


def _shipkia_recent_text_messages(conversation: str | None, limit: int = 40) -> list[Any]:
    if not conversation:
        return []
    try:
        rows = safe_ai_get_all(
            "Chat Message",
            filters={
                "conversation": conversation,
                "content_type": "Text",
            },
            fields=["name", "direction", "body", "creation"],
            order_by="creation desc",
            limit=limit,
        )
    except Exception:
        return []
    return list(reversed(rows or []))


def _shipkia_message_body(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("body") or "").strip()
    return str(getattr(row, "body", "") or "").strip()


def _shipkia_message_direction(row: Any) -> str:
    if isinstance(row, dict):
        return str(row.get("direction") or "").strip()
    return str(getattr(row, "direction", "") or "").strip()


def _shipkia_message_creation(row: Any) -> Any:
    if isinstance(row, dict):
        return row.get("creation")
    return getattr(row, "creation", None)


def _shipkia_current_message_in_rows(rows: list[Any], text: str) -> bool:
    current_text = (text or "").strip()
    if not current_text:
        return False
    return any(
        _shipkia_message_direction(row).lower() == "inbound"
        and _shipkia_message_body(row) == current_text
        for row in rows
    )


def _shipkia_row_recent_enough(row: Any) -> bool:
    creation = _shipkia_message_creation(row)
    if not creation:
        return True
    try:
        creation_dt = get_datetime(creation)
        if not creation_dt:
            return True
        age_seconds = (now_datetime() - creation_dt).total_seconds()
    except Exception:
        return True
    return age_seconds <= SHIPKIA_WORKFLOW_LOOKBACK_SECONDS


def _extract_shipkia_workflow_context(
    text: str,
    conversation: str | None,
) -> dict[str, Any]:
    rows = _shipkia_recent_text_messages(conversation)
    stored_state = _shipkia_load_conversation_state(conversation)
    if text and not _shipkia_current_message_in_rows(rows, text):
        rows.append(
            {
                "direction": "Inbound",
                "body": text,
                "creation": now_datetime(),
            }
        )

    latest_prompt_index = -1
    latest_prompt_kind = ""
    latest_workflow_start = -1
    latest_reset_index = -1
    for index, row in enumerate(rows):
        direction = _shipkia_message_direction(row).lower()
        body = _shipkia_message_body(row)
        if direction == "inbound" and _shipkia_reset_intent(body):
            latest_reset_index = index
        if direction != "outbound":
            continue
        kind = _shipkia_prompt_kind(body)
        if not kind:
            continue
        if _shipkia_row_recent_enough(row):
            latest_prompt_index = index
            latest_prompt_kind = kind
        if kind == "business_type" and _shipkia_row_recent_enough(row):
            latest_workflow_start = index

    if not latest_prompt_kind and stored_state.get("latest_prompt"):
        latest_prompt_kind = str(stored_state.get("latest_prompt") or "")
    if stored_state.get("state_closed") and not stored_state.get("pending_slots"):
        latest_prompt_index = -1
        latest_prompt_kind = ""
    active = latest_prompt_index >= 0 or bool(stored_state.get("pending_slots"))
    start_index = max(latest_workflow_start, latest_reset_index + 1) if latest_reset_index >= 0 else latest_workflow_start if latest_workflow_start >= 0 else 0
    context: dict[str, Any] = {
        "active": active,
        "latest_prompt": latest_prompt_kind,
        "pending_slots": list(stored_state.get("pending_slots") or []),
        "pending_confirmation": dict(stored_state.get("pending_confirmation") or {}),
        "rate_details": {},
        "reset_requested": latest_reset_index >= 0,
    }
    expected = ""

    for row in rows[start_index:]:
        direction = _shipkia_message_direction(row).lower()
        body = _shipkia_message_body(row)
        if not body:
            continue
        if direction == "outbound":
            prompt_kind = _shipkia_prompt_kind(body)
            if prompt_kind:
                expected = prompt_kind
            continue
        if direction != "inbound":
            continue

        pending_slots = _shipkia_pending_slots_for_step(expected, context) or list(context.get("pending_slots") or [])
        pending_updates = _shipkia_extract_pending_slot_updates(body, pending_slots)
        _shipkia_apply_slot_updates(context, pending_updates)

        business_type = _shipkia_parse_business_type(body)
        if business_type:
            context["business_type"] = business_type
        elif expected == "business_type":
            continue

        compact_profile = _shipkia_extract_compact_profile(
            body,
            expected=expected in {"business_type", "compact_profile", "business_name", "current_aggregator_status", "current_aggregator_name", "monthly_shipments"},
        )
        for key, value in compact_profile.items():
            if value not in (None, "") and key not in context:
                context[key] = value

        if expected == "business_name" and "business_name" not in context:
            business_name = _shipkia_safe_free_text(body)
            if business_name:
                context["business_name"] = business_name
        if expected == "business_operation_mode" and "business_operation_mode" not in context:
            operation_mode = _shipkia_safe_free_text(body)
            if operation_mode:
                context["business_operation_mode"] = operation_mode
        if expected == "current_aggregator_status" and "current_aggregator_status" not in context:
            aggregator_status = _shipkia_parse_yes_no(body)
            if aggregator_status:
                context["current_aggregator_status"] = aggregator_status
        if expected == "current_aggregator_name" and "current_aggregator_name" not in context:
            aggregator_name = _shipkia_safe_free_text(body)
            if aggregator_name:
                context["current_aggregator_name"] = aggregator_name
        if expected in {"current_rate_rto", "current_shipping_rate", "rto_percentage"} and "current_shipping_rate" not in context:
            current_rate = _shipkia_parse_current_shipping_rate(body, expected=True)
            if current_rate is not None:
                context["current_shipping_rate"] = current_rate
        if expected in {"current_rate_rto", "current_shipping_rate", "rto_percentage"} and "rto_percentage" not in context:
            rto_percentage = _shipkia_parse_rto_percentage(body, expected=True)
            if rto_percentage is not None:
                context["rto_percentage"] = rto_percentage

        monthly_shipments = _shipkia_parse_monthly_shipments(
            body,
            expected=expected == "monthly_shipments",
        )
        if monthly_shipments is not None:
            context["monthly_shipments"] = monthly_shipments

        rate_details = _extract_shipkia_rate_details(body)
        if expected == "pickup_delivery" and not (
            rate_details.get("pickup_city")
            or rate_details.get("delivery_city")
            or rate_details.get("delivery_scope")
            or rate_details.get("pickup_pincode")
            or rate_details.get("delivery_pincode")
        ):
            location = _shipkia_single_location_answer(body)
            if location:
                if location.get("pincode"):
                    if not context["rate_details"].get("pickup_pincode"):
                        context["rate_details"]["pickup_pincode"] = location["pincode"]
                    elif not context["rate_details"].get("delivery_pincode"):
                        context["rate_details"]["delivery_pincode"] = location["pincode"]
                elif location.get("city"):
                    if not context["rate_details"].get("pickup_city"):
                        context["rate_details"]["pickup_city"] = location["city"]
                    elif not context["rate_details"].get("delivery_city"):
                        context["rate_details"]["delivery_city"] = location["city"]

        has_route_details = bool(
            (rate_details.get("pickup_city") or rate_details.get("pickup_pincode"))
            and (
                rate_details.get("delivery_city")
                or rate_details.get("delivery_pincode")
                or rate_details.get("delivery_scope")
            )
        )
        collect_rate_details = (
            has_route_details
            or bool(rate_details.get("rate_type") == "flat_rate")
            or expected in SHIPKIA_RATE_PROMPT_MARKERS
            or (
                rate_details.get("rate_intent")
                and expected
                not in {
                    "business_name",
                    "business_operation_mode",
                    "current_aggregator_status",
                    "current_aggregator_name",
                    "current_rate_rto",
                    "current_shipping_rate",
                    "rto_percentage",
                    "monthly_shipments",
                }
            )
        )
        if collect_rate_details:
            for field in (
                "pickup_city",
                "delivery_city",
                "pickup_pincode",
                "delivery_pincode",
                "delivery_scope",
                "weight_grams",
                "payment_type",
                "order_value",
                "zone",
                "rate_type",
            ):
                if rate_details.get(field):
                    context["rate_details"][field] = rate_details[field]

    return context


def _shipkia_next_workflow_step(context: dict[str, Any]) -> str:
    if not context.get("business_type"):
        return "business_type"
    needs_profile = (
        not context.get("business_name")
        or not context.get("current_aggregator_status")
        or (
            context.get("current_aggregator_status") == "Yes"
            and not context.get("current_aggregator_name")
        )
    )
    if needs_profile:
        return "compact_profile"
    if context.get("current_aggregator_status") == "Yes" and (
        not _shipkia_has_positive_number(context.get("current_shipping_rate"))
        or not _shipkia_has_positive_number(context.get("rto_percentage"))
    ):
        if not _shipkia_has_positive_number(context.get("current_shipping_rate")):
            return "current_shipping_rate"
        return "rto_percentage"
    if not _shipkia_has_monthly_shipments(context):
        return "monthly_shipments"
    return ""


def _shipkia_repeated_question_blocked(
    question_step: str,
    context: dict[str, Any],
    latest_text: str,
    rate_details: dict[str, Any],
) -> bool:
    if not question_step or question_step != str(context.get("latest_prompt") or ""):
        return False
    if _shipkia_text_answers_prompt(latest_text, question_step, rate_details):
        return False
    if _shipkia_text_has_customer_update(latest_text, rate_details):
        return False
    return True


def _shipkia_next_workflow_question(
    context: dict[str, Any],
    conversation: str | None = None,
    latest_text: str = "",
    rate_details: dict[str, Any] | None = None,
) -> str:
    step = _shipkia_next_workflow_step(context)
    if not step:
        _shipkia_save_conversation_state(conversation, "", context)
        return ""
    question_step = step
    if step == "compact_profile":
        pending_slots = _shipkia_pending_slots_for_step(step, context)
        pending = set(pending_slots)
        if pending_slots == ["business_name"]:
            question_step = "business_name_only"
        elif pending_slots == ["monthly_shipments"]:
            question_step = "monthly_shipments"
        elif pending_slots == ["current_aggregator_status"]:
            question_step = "current_aggregator_status_only"
        elif pending_slots == ["current_aggregator_name"]:
            question_step = "current_aggregator_name"
        elif pending == {"business_name", "monthly_shipments"}:
            question_step = "profile_business_monthly"
        elif "business_name" in pending and (
            "current_aggregator_status" in pending or "current_aggregator_name" in pending
        ):
            question_step = "profile_business_aggregator"
        elif "monthly_shipments" in pending and (
            "current_aggregator_status" in pending or "current_aggregator_name" in pending
        ):
            question_step = "profile_monthly_aggregator"
    if step in {"current_shipping_rate", "rto_percentage"}:
        missing_rate = not _shipkia_has_positive_number(context.get("current_shipping_rate"))
        missing_rto = not _shipkia_has_positive_number(context.get("rto_percentage"))
        if missing_rate and missing_rto:
            question_step = "current_rate_rto"
    if step == "weight_grams":
        pending_slots = _shipkia_pending_slots_for_step(step, context)
        if pending_slots == ["weight_grams"]:
            question_step = "weight_only"
    if _shipkia_repeated_question_blocked(question_step, context, latest_text, rate_details or {}):
        _shipkia_save_conversation_state(conversation, question_step, context)
        return _shipkia_render_reply_template("repeat_question_blocked", conversation, context)
    _shipkia_save_conversation_state(conversation, question_step, context)
    return _shipkia_render_reply_template(question_step, conversation, context)


def _shipkia_text_answers_prompt(text: str, prompt_kind: str, rate_details: dict[str, Any]) -> bool:
    if not prompt_kind:
        return False
    if prompt_kind == "business_type":
        return bool(_shipkia_parse_business_type(text))
    if prompt_kind in {
        "compact_profile",
        "business_name_only",
        "profile_business_monthly",
        "profile_business_aggregator",
        "profile_monthly_aggregator",
    }:
        profile = _shipkia_extract_compact_profile(text, expected=True)
        return bool(
            profile.get("business_name")
            or profile.get("monthly_shipments")
            or profile.get("current_aggregator_status")
            or profile.get("current_aggregator_name")
        )
    if prompt_kind == "current_aggregator_status_only":
        return bool(_shipkia_parse_yes_no(text))
    if prompt_kind in {"business_name", "business_operation_mode", "current_aggregator_name"}:
        return bool(_shipkia_safe_free_text(text))
    if prompt_kind == "current_aggregator_status":
        return bool(_shipkia_parse_yes_no(text))
    if prompt_kind in {"current_rate_rto", "current_shipping_rate"}:
        return (
            _shipkia_parse_current_shipping_rate(text, expected=True) is not None
            or _shipkia_parse_rto_percentage(text, expected=True) is not None
        )
    if prompt_kind == "rto_percentage":
        return (
            _shipkia_parse_current_shipping_rate(text, expected=True) is not None
            or _shipkia_parse_rto_percentage(text, expected=True) is not None
        )
    if prompt_kind == "monthly_shipments":
        return _shipkia_parse_monthly_shipments(text, expected=True) is not None
    if prompt_kind == "pickup_delivery":
        return bool(
            rate_details.get("pickup_city")
            or rate_details.get("delivery_city")
            or rate_details.get("delivery_scope")
            or rate_details.get("pickup_pincode")
            or rate_details.get("delivery_pincode")
        )
    if prompt_kind in {"weight_grams", "weight_only"}:
        return bool(rate_details.get("weight_grams"))
    if prompt_kind == "payment_type":
        return bool(rate_details.get("payment_type"))
    if prompt_kind == "order_value":
        return bool(rate_details.get("order_value"))
    return False


def _shipkia_post_rate_profile_nudge(conversation: str | None, context: dict[str, Any] | None = None) -> str:
    context = context or {}
    if context.get("business_name") and _shipkia_has_monthly_shipments(context):
        return ""
    return _shipkia_render_reply_template("soft_profile_after_rate", conversation, context)


def _shipkia_route_starting_rate_reply(
    text: str,
    details: dict[str, Any],
    conversation: str | None,
    context: dict[str, Any] | None = None,
) -> str:
    has_city_route = bool(details.get("pickup_city") and details.get("delivery_city"))
    has_pan_india_scope = bool(
        (details.get("pickup_city") or details.get("pickup_pincode"))
        and details.get("delivery_scope")
    )
    if not details.get("rate_intent") or not (has_city_route or has_pan_india_scope):
        return ""

    payment_type = details.get("payment_type")
    if payment_type not in {"Prepaid", "COD", "Both"}:
        return ""
    tool_payment_type = "Prepaid" if payment_type == "Both" else payment_type
    weight_grams = details.get("weight_grams") or 500
    tool_args = {
        "pickup_city": details.get("pickup_city"),
        "weight_grams": weight_grams,
        "payment_type": tool_payment_type,
        "direction": "FWD",
        "conversation": conversation,
    }
    if details.get("pickup_pincode"):
        tool_args["pickup_pincode"] = details.get("pickup_pincode")
    if has_city_route:
        tool_args["delivery_city"] = details.get("delivery_city")
        if details.get("delivery_pincode"):
            tool_args["delivery_pincode"] = details.get("delivery_pincode")
    elif has_pan_india_scope:
        tool_args["delivery_scope"] = "Pan India"
        tool_args["zone"] = "A"
        tool_args["query"] = "Pan India"
    if tool_payment_type == "COD" and details.get("order_value"):
        tool_args["order_value"] = details.get("order_value")

    try:
        tool_response = execute_mcp_tool(SHIPKIA_RATE_TOOL_NAME, tool_args)
        result = json.loads(tool_response) if isinstance(tool_response, str) else tool_response
    except Exception:
        log_agent_event(
            "MCP Tool",
            "Failed",
            conversation=conversation,
            tool_name=SHIPKIA_RATE_TOOL_NAME,
            request=tool_args,
            error_message="ShipKia starting-rate teaser failed.",
            traceback=frappe.get_traceback(),
        )
        return ""

    if not isinstance(result, dict) or result.get("status") != "success":
        return ""
    options = result.get("options") if isinstance(result.get("options"), list) else []
    starting_rate = result.get("starting_rate") if has_pan_india_scope else None
    if starting_rate in (None, "") and options and isinstance(options[0], dict):
        first_option = options[0]
        quote = first_option.get("quote") if isinstance(first_option.get("quote"), dict) else {}
        starting_rate = quote.get("total") or quote.get("total_before_cod") or first_option.get("starting_total")
    if starting_rate in (None, ""):
        return ""

    pickup = details.get("pickup_city")
    delivery = details.get("delivery_city") or details.get("delivery_scope") or "Pan India"
    payment_label = "prepaid/COD" if payment_type == "Both" else str(payment_type).lower()
    reply = _shipkia_render_reply_template(
        "route_starting_rate",
        conversation,
        context or {},
        pickup=pickup,
        delivery=delivery,
        weight_grams=weight_grams,
        payment_label=payment_label,
        starting_rate=starting_rate,
    )
    nudge = _shipkia_post_rate_profile_nudge(conversation, context)
    return f"{reply}\n\n{nudge}".strip() if nudge else reply


def _build_shipkia_rate_autoreply(text: str, conversation: str | None = None) -> tuple[str, bool]:
    details = _extract_shipkia_rate_details(text)
    chat_context = _extract_shipkia_workflow_context(text, conversation)
    exact_rate_intent = _shipkia_exact_rate_intent(text)
    onboarding_intent = _shipkia_onboarding_intent(text)
    rate_card_request = _shipkia_rate_card_request(text)
    price_objection_intent = _shipkia_price_objection_intent(text)
    customer_memory_intent = _shipkia_customer_memory_intent(text)
    negotiation_intent = _shipkia_negotiation_intent(text)
    frustration_intent = _shipkia_frustration_intent(text)
    reset_intent = _shipkia_reset_intent(text)
    lead_name, _convo, contact = _shipkia_resolve_lead(
        conversation,
        create_if_missing=bool(
            details.get("rate_intent")
            or chat_context.get("active")
            or exact_rate_intent
            or onboarding_intent
            or rate_card_request
            or price_objection_intent
            or customer_memory_intent
            or negotiation_intent
            or frustration_intent
            or _shipkia_correction_intent(text)
            or reset_intent
        ),
    )
    saved_context = _shipkia_load_lead_context(lead_name)
    if reset_intent:
        saved_context = {
            key: value
            for key, value in saved_context.items()
            if key in {"lead_name", "customer_name"}
        }
    context = _shipkia_merge_saved_context(saved_context, chat_context)
    active_workflow = bool(context.get("active"))
    router_decision = _shipkia_route_user_message(
        text,
        conversation,
        saved_context,
        chat_context,
        details,
    )
    router_intent = str(router_decision.get("intent") or "")

    if _shipkia_is_greeting(text) and not details.get("rate_intent"):
        if saved_context:
            return _shipkia_greeting_reply(saved_context, contact, conversation), True
        return _shipkia_render_reply_template("greeting_unknown", conversation, context), True

    if reset_intent:
        _shipkia_save_conversation_state(conversation, "business_type", context)
        return _shipkia_render_reply_template("business_type", conversation, context), True

    pending_confirmation = chat_context.get("pending_confirmation") if isinstance(chat_context.get("pending_confirmation"), dict) else {}
    if pending_confirmation:
        if _shipkia_confirmation_yes(text):
            pending_entities = pending_confirmation.get("entities") if isinstance(pending_confirmation.get("entities"), dict) else {}
            confirmed_decision = {
                "entities": pending_entities,
                "field_confidence": {
                    key: 1.0
                    for key in pending_entities
                    if key != "rate_details"
                },
            }
            if isinstance(pending_entities.get("rate_details"), dict):
                for rate_key in pending_entities.get("rate_details") or {}:
                    confirmed_decision["field_confidence"][f"rate_details.{rate_key}"] = 1.0
            applied = _shipkia_apply_router_entities(context, confirmed_decision, min_confidence=0.0)
            if lead_name and applied:
                _shipkia_save_lead_context(
                    lead_name,
                    context,
                    field_confidence=confirmed_decision.get("field_confidence") or {},
                    only_confident=True,
                )
                if applied.get("rate_details"):
                    _shipkia_save_rate_details_to_lead(lead_name, context.get("rate_details") or {})
            _shipkia_save_conversation_state(conversation, "", context)
            return _shipkia_correction_saved_reply(applied), True
        if _shipkia_confirmation_no(text):
            _shipkia_save_conversation_state(conversation, "", context)
            return "Theek hai, maine woh update save nahi kiya.", True
        _shipkia_save_conversation_state(conversation, "", context)

    if router_intent == "correction_update":
        applied = _shipkia_apply_router_entities(context, router_decision)
        if lead_name and applied:
            _shipkia_save_lead_context(
                lead_name,
                context,
                field_confidence=router_decision.get("field_confidence") or {},
                only_confident=True,
            )
            if applied.get("rate_details"):
                _shipkia_save_rate_details_to_lead(lead_name, context.get("rate_details") or {})
        _shipkia_save_conversation_state(conversation, "", context)
        return _shipkia_correction_saved_reply(applied), True

    if router_intent == "correction_unclear":
        _shipkia_save_conversation_state(conversation, "", context)
        return "Mujhe correction clear nahi hua. Aap ek baar updated detail clearly share kar dijiye.", True

    if (
        _shipkia_slot_only_rate_answer(details)
        and not active_workflow
        and _shipkia_completed_rate_details(context.get("rate_details") or (saved_context or {}).get("rate_details"))
    ):
        _shipkia_save_conversation_state(conversation, "", context)
        return "Noted. Maine payment/weight detail update kar li hai. ShipKia team exact check ke liye help kar sakti hai.", True

    if (
        router_intent == "qualification_update"
        and not router_decision.get("should_save")
        and {
            key: value
            for key, value in (router_decision.get("entities") or {}).items()
            if key != "rate_details" and value not in (None, "")
        }
    ):
        _shipkia_save_pending_confirmation(conversation, router_decision)
        return _shipkia_low_confidence_update_reply(router_decision), True

    if customer_memory_intent or router_intent == "saved_info_question":
        _shipkia_save_conversation_state(conversation, "", context)
        memory_context = dict(context)
        for key, value in (saved_context or {}).items():
            if value not in (None, ""):
                memory_context[key] = value
        return _shipkia_customer_memory_reply(text, memory_context, conversation), True

    if router_intent == "general_shipkia_question":
        _shipkia_save_conversation_state(conversation, "", context)
        return _shipkia_feature_reply(text), True

    if onboarding_intent:
        if lead_name:
            _shipkia_mark_signup_link_sent(lead_name)
        _shipkia_save_conversation_state(conversation, "", context)
        return _shipkia_render_reply_template("onboarding_help", conversation, context), True

    if rate_card_request:
        _shipkia_save_conversation_state(conversation, "callback_time", context)
        return _shipkia_render_reply_template("rate_card_request", conversation, context), True

    if price_objection_intent or negotiation_intent:
        if lead_name:
            _shipkia_save_lead_context(lead_name, context)
        _shipkia_save_conversation_state(conversation, "callback_time", context)
        monthly_shipments = context.get("monthly_shipments")
        monthly_label = (
            f"{_shipkia_format_value(monthly_shipments)}"
            if monthly_shipments not in (None, "", 0, "0")
            else "aapke"
        )
        aggregator_label = context.get("current_aggregator_name") or "aapke current setup"
        return (
            _shipkia_render_reply_template(
                "custom_pricing_review",
                conversation,
                context,
                monthly_shipments_label=monthly_label,
                aggregator_label=aggregator_label,
            ),
            True,
        )

    if frustration_intent:
        if lead_name:
            _shipkia_save_lead_context(lead_name, context)
            _shipkia_mark_human_review_needed(lead_name, str(text or "").strip())
        next_question = _shipkia_next_workflow_question(context, conversation, text, details)
        if next_question:
            return (
                _shipkia_render_reply_template(
                    "frustration_recovery",
                    conversation,
                    context,
                    next_question=next_question,
                ),
                True,
            )
        _shipkia_save_conversation_state(conversation, "", context)
        return (
            "Sorry, I understand this got repetitive. Maine shared details note kar li hain; ab unhi details ke basis par continue karti hoon.",
            True,
        )

    if str(context.get("latest_prompt") or "") == "callback_time" and not details.get("rate_intent"):
        if _shipkia_callback_yes(text):
            _shipkia_save_conversation_state(conversation, "callback_time", context)
            return _shipkia_render_reply_template("callback_time", conversation, context), True
        if _shipkia_callback_no(text):
            _shipkia_save_conversation_state(conversation, "", context)
            return "No problem. Starting rates ke liye route aur approx weight share kar sakte hain.", True
        callback_time = _shipkia_extract_callback_time(text)
        if callback_time:
            _shipkia_save_callback_request(lead_name, callback_time)
            _shipkia_save_conversation_state(conversation, "", context)
            return (
                _shipkia_render_reply_template(
                    "callback_saved",
                    conversation,
                    context,
                    callback_time=callback_time,
                ),
                True,
            )
        _shipkia_save_conversation_state(conversation, "", context)
        return "", False

    if exact_rate_intent:
        callback_time = _shipkia_extract_callback_time(text)
        if callback_time and _shipkia_callback_yes(text):
            _shipkia_save_callback_request(lead_name, callback_time)
            return (
                _shipkia_render_reply_template(
                    "callback_saved",
                    conversation,
                    context,
                    callback_time=callback_time,
                ),
                True,
            )
        return _shipkia_render_reply_template("exact_rate_callback_offer", conversation, context), True

    if (
        _shipkia_acknowledgement(text)
        and not details.get("rate_intent")
        and not active_workflow
        and (context.get("rate_details") or (saved_context or {}).get("rate_details"))
    ):
        _shipkia_save_conversation_state(conversation, "", context)
        return _shipkia_render_reply_template("acknowledgement", conversation, context), True

    if not details.get("rate_intent") and not active_workflow:
        return "", False
    if (
        not details.get("rate_intent")
        and active_workflow
        and not _shipkia_text_answers_prompt(
            text,
            str(context.get("latest_prompt") or ""),
            details,
        )
        and not _shipkia_text_has_customer_update(text, details)
    ):
        return "", False

    applied_entities: dict[str, Any] = {}
    if router_decision.get("should_save"):
        applied_entities = _shipkia_apply_router_entities(context, router_decision)

    if lead_name:
        _shipkia_save_lead_context(lead_name, context)

    latest_prompt = str(context.get("latest_prompt") or "")
    answers_rate_prompt = _shipkia_text_answers_prompt(text, latest_prompt, details)
    if active_workflow and _shipkia_refuses_requested_info(text) and not answers_rate_prompt:
        _shipkia_save_conversation_state(conversation, "", context)
        return _shipkia_render_reply_template("information_refused", conversation, context), True

    if (
        _shipkia_slot_only_rate_answer(details)
        and latest_prompt not in SHIPKIA_RATE_PROMPT_MARKERS
        and _shipkia_completed_rate_details(context.get("rate_details") or (saved_context or {}).get("rate_details"))
    ):
        _shipkia_save_conversation_state(conversation, "", context)
        return "Noted. Maine ye detail update kar li hai.", True

    current_rate_slot_present = _shipkia_rate_details_have_customer_slot(details) or bool(
        details.get("payment_type") or details.get("order_value") or details.get("rate_intent")
    )

    can_merge_previous_rate_slots = bool(
        router_decision.get("should_calculate_rate")
        and (
            details.get("rate_intent")
            or answers_rate_prompt
            or current_rate_slot_present
        )
    )
    merged_rate_details = (
        dict(context.get("rate_details") or {})
        if can_merge_previous_rate_slots
        else {}
    )
    for field in (
        "pickup_city",
        "delivery_city",
        "pickup_pincode",
        "delivery_pincode",
        "delivery_scope",
        "weight_grams",
        "payment_type",
        "order_value",
        "zone",
        "rate_type",
    ):
        if details.get(field):
            merged_rate_details[field] = details[field]
    if details.get("rate_intent") or merged_rate_details:
        merged_rate_details["rate_intent"] = True
    if merged_rate_details:
        context["rate_details"] = merged_rate_details
        if lead_name:
            _shipkia_save_rate_details_to_lead(lead_name, merged_rate_details)

    rate_flow = bool(
        details.get("rate_intent")
        or merged_rate_details
        or answers_rate_prompt
        or current_rate_slot_present
    )
    if applied_entities and not rate_flow:
        _shipkia_save_conversation_state(conversation, "", context)
        return _shipkia_correction_saved_reply(applied_entities), True

    if rate_flow:
        details = merged_rate_details or details
        quote_details_present = any(
            details.get(key)
            for key in (
                "pickup_city",
                "delivery_city",
                "pickup_pincode",
                "delivery_pincode",
                "delivery_scope",
                "weight_grams",
                "payment_type",
            )
        )
        if details.get("rate_type") == "flat_rate" and not quote_details_present:
            _shipkia_save_conversation_state(conversation, "weight_grams", context)
            return _shipkia_render_reply_template("weight_grams", conversation, context), True

        followup = _shipkia_rate_followup(details, conversation, context)
        if followup:
            return followup, True

        starting_reply = _shipkia_route_starting_rate_reply(text, details, conversation, context)
        if starting_reply:
            if lead_name:
                _shipkia_mark_rate_shared(lead_name)
            _shipkia_save_conversation_state(conversation, "", context)
            return starting_reply, True

        return _shipkia_render_reply_template("exact_rate_callback_offer", conversation, context), True

    workflow_question = _shipkia_next_workflow_question(context, conversation, text, details)
    if workflow_question:
        return workflow_question, True

    quote_details_present = any(
        details.get(key)
        for key in (
            "pickup_city",
            "delivery_city",
            "pickup_pincode",
            "delivery_pincode",
            "delivery_scope",
            "weight_grams",
            "payment_type",
        )
    )
    if details.get("rate_type") == "flat_rate" and not quote_details_present:
        return _shipkia_render_reply_template("weight_grams", conversation, context), True

    followup = _shipkia_rate_followup(details, conversation, context)
    if followup:
        return followup, True

    starting_reply = _shipkia_route_starting_rate_reply(text, details, conversation, context)
    if starting_reply:
        if lead_name:
            _shipkia_mark_rate_shared(lead_name)
        _shipkia_save_conversation_state(conversation, "", context)
        return starting_reply, True

    return _shipkia_render_reply_template("exact_rate_callback_offer", conversation, context), True


def _conversation_owned_by_order_confirmation(conversation: str | None) -> bool:
    """Let Confluence own active order-confirmation chats.

    WA Chat Hub remains responsible for receiving/sending messages, but its
    generic autopilot should not also answer a conversation that an active
    order-confirmation workflow is handling.
    """
    if not conversation:
        return False
    try:
        if not frappe.db.exists("DocType", "Order Confirmation Workflow"):
            return False
        return bool(
            frappe.db.exists(
                "Order Confirmation Workflow",
                {
                    "chat_conversation": conversation,
                    "status": [
                        "not in",
                        [
                            "Confirmed",
                            "Issue Created",
                            "Level 3 Ticket Created",
                            "Failed",
                            "Cancelled",
                        ],
                    ],
                },
            )
        )
    except Exception:
        return False


def on_message_received(doc, method):
    """Fallback when Chat Message is inserted outside append_message()."""
    if _inside_append_message():
        return
    if (doc.direction or "").strip() != "Inbound":
        return
    if (doc.sender_type or "").strip() in ("AI", "System", "Bot"):
        return
    schedule_autopilot_for_message(doc.name)


def schedule_autopilot_for_message(message_name: str) -> None:
    """Queue AI autopilot after inbound message, lead link, and messaging window are ready."""
    if not message_name or not safe_ai_exists("Chat Message", message_name):
        return

    doc = safe_ai_get_doc("Chat Message", message_name)
    if doc.direction != "Inbound":
        return
    if not _inbound_triggers_autopilot(doc):
        return
    if _conversation_owned_by_order_confirmation(doc.conversation):
        _log_ai_timing(
            "skip",
            message=message_name,
            conversation=getattr(doc, "conversation", None),
            reason="order_confirmation_workflow",
        )
        return

    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    if not settings.enable_ai_autopilot:
        return
    if (settings.autopilot_mode or "Suggest Only") == "Disabled":
        return

    try:
        if _autopilot_batching_enabled(settings):
            enqueue(
                "wa_chat_hub.api.ai_bot.process_conversation",
                queue="short",
                conversation=doc.conversation,
                trigger_message_id=message_name,
                enqueue_after_commit=True,
                now=False,
                job_id=f"wa_ai_autopilot_conversation_{doc.conversation}_{message_name}",
                deduplicate=True,
            )
            _log_ai_timing(
                "enqueue",
                message=message_name,
                conversation=getattr(doc, "conversation", None),
                content_type=getattr(doc, "content_type", None) or "Text",
                queue="short",
                mode="conversation_batch",
            )
            return

        enqueue(
            "wa_chat_hub.api.ai_bot.process_message",
            queue="short",
            message_id=message_name,
            enqueue_after_commit=True,
            now=False,
            job_id=f"wa_ai_autopilot_{message_name}",
            deduplicate=True,
        )
        _log_ai_timing(
            "enqueue",
            message=message_name,
            conversation=getattr(doc, "conversation", None),
            content_type=getattr(doc, "content_type", None) or "Text",
            queue="short",
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA AI Autopilot Enqueue Failed")


def process_conversation(conversation: str, trigger_message_id: str | None = None):
    total_started = time.monotonic()
    set_service_user_context("ai_autopilot")
    set_ai_security_context(
        operation="ai_autopilot_batch",
        conversation=conversation,
        message=trigger_message_id or "",
    )

    if not conversation:
        return

    if not _conversation_allows_autopilot(conversation):
        _log_ai_timing(
            "skip",
            message=trigger_message_id,
            conversation=conversation,
            reason="conversation_not_open",
            total_sec=elapsed(total_started),
        )
        return

    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    if not settings.enable_ai_autopilot:
        return
    if (settings.autopilot_mode or "Suggest Only") == "Disabled":
        return

    _, total_wait = _wait_for_conversation_batch_window(conversation, settings)

    with filelock(_autopilot_batch_lock_name(conversation), timeout=AUTOPILOT_BATCH_LOCK_TIMEOUT):
        msg_doc = _latest_autopilot_inbound(conversation)
        if not msg_doc:
            _log_ai_timing(
                "skip",
                message=trigger_message_id,
                conversation=conversation,
                reason="no_inbound_for_batch",
                total_sec=elapsed(total_started),
            )
            return

        if _autopilot_batch_already_answered(conversation, msg_doc):
            _log_ai_timing(
                "skip",
                message=msg_doc.name,
                trigger_message=trigger_message_id,
                conversation=conversation,
                reason="batch_already_answered",
                waited_sec=total_wait,
                total_sec=elapsed(total_started),
            )
            return

        if trigger_message_id and msg_doc.name != trigger_message_id:
            _log_ai_timing(
                "batch_selected_latest",
                message=msg_doc.name,
                trigger_message=trigger_message_id,
                conversation=conversation,
                waited_sec=total_wait,
            )

        return process_message(msg_doc.name, skip_batch_wait=True)


def process_message(message_id, skip_batch_wait: bool = False):
    total_started = time.monotonic()
    set_service_user_context("ai_autopilot")

    msg_doc = safe_ai_get_doc("Chat Message", message_id)
    frappe.flags.wa_ai_reply_to_message = str(message_id)
    frappe.local.wa_ai_reply_to_message = str(message_id)
    conversation = str(getattr(msg_doc, "conversation", "") or "")
    set_ai_security_context(
        operation="ai_autopilot",
        conversation=conversation,
        message=str(message_id),
    )
    _log_ai_timing(
        "start",
        message=message_id,
        conversation=conversation,
        queue_wait_sec=queue_wait_seconds(msg_doc.creation),
        content_type=msg_doc.content_type or "Text",
    )

    if not _conversation_allows_autopilot(conversation):
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="conversation_not_open",
            total_sec=elapsed(total_started),
        )
        return

    if _conversation_owned_by_order_confirmation(conversation):
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="order_confirmation_workflow",
            total_sec=elapsed(total_started),
        )
        return

    if _already_replied_to_inbound(conversation, message_id):
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="already_replied",
            total_sec=elapsed(total_started),
        )
        return

    from wa_chat_hub.messaging.windows import evaluate_send_permission

    window_decision = evaluate_send_permission(conversation, "Text")
    if not window_decision.can_send_free_form:
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="messaging_window_closed",
            total_sec=elapsed(total_started),
        )
        frappe.log_error(
            f"Conversation {conversation}: {window_decision.reason}",
            "WA AI Autopilot Skipped (Messaging Window Closed)",
        )
        return

    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    if not skip_batch_wait:
        if _autopilot_batching_enabled(settings):
            return process_conversation(conversation, trigger_message_id=message_id)
        superseded, delay_seconds = _wait_for_autopilot_batch_window(msg_doc, settings)
        if superseded:
            _log_ai_timing(
                "skip",
                message=message_id,
                conversation=conversation,
                reason="superseded_by_newer_inbound",
                delay_seconds=delay_seconds,
                content_type=msg_doc.content_type or "Text",
                total_sec=elapsed(total_started),
            )
            return

    body_text = str(getattr(msg_doc, "body", "") or "").strip()
    content_type = str(getattr(msg_doc, "content_type", "Text") or "Text").title()
    media_url = str(getattr(msg_doc, "media_url", "") or "").strip()

    conversation_context = cast(
        dict[str, Any],
        safe_ai_get_value(
            "Chat Conversation",
            conversation,
            ["channel_account", "department"],
            as_dict=True,
        )
        or {},
    )
    channel_account = str(conversation_context.get("channel_account") or "")
    department = str(conversation_context.get("department") or "")
    mcp_agent_runtime = _channel_prefers_mcp_agent(channel_account, settings)
    is_shipkia_channel = "shipkia" in channel_account.lower()

    if is_shipkia_channel:
        _reset_current_reply_tool_state()
        shipkia_body_text = (
            _shipkia_combined_inbound_since_last_outbound(
                conversation,
                getattr(msg_doc, "creation", None),
            )
            or body_text
        )
        response_text, handled_rate = _build_shipkia_rate_autoreply(shipkia_body_text, conversation)
        if handled_rate:
            reply_intent = _polish_autopilot_reply((response_text or "").strip())
            llm_response_text = _shipkia_llm_reply_from_intent(
                reply_intent,
                shipkia_body_text,
                conversation,
                channel_account,
                settings,
                message_id=str(message_id),
            )
            response_text = llm_response_text or reply_intent
            guarded_response, rate_guard_blocked = _guard_unverified_rate_reply(
                response_text,
                latest_user_text=shipkia_body_text,
            )
            if rate_guard_blocked:
                response_text = guarded_response
            mode = _deliver_or_draft_ai_reply(conversation, response_text, settings, message_id) or "duplicate_skip"
            _log_ai_timing(
                "total_done",
                message=message_id,
                conversation=conversation,
                mode=f"shipkia_rate_{mode}",
                total_sec=elapsed(total_started),
            )
            return

    quick_media_reply = _quick_media_only_reply(content_type, body_text, media_url)
    if quick_media_reply:
        mode = _deliver_or_draft_ai_reply(conversation, quick_media_reply, settings, message_id) or "duplicate_skip"
        _log_ai_timing(
            "total_done",
            message=message_id,
            conversation=conversation,
            mode=f"quick_media_{mode}",
            total_sec=elapsed(total_started),
        )
        return

    context_started = time.monotonic()
    history = _load_recent_conversation_history(conversation)
    history_before_current = [row for row in history if str(row.name) != str(message_id)]

    set_ai_security_context(channel_account=channel_account)
    prompt_config = get_effective_prompt_config(channel_account)

    media_context = ""
    use_vision_for_image = False
    media_fallback_reply = ""
    if media_url and content_type in MEDIA_CONTENT_TYPES:
        try:
            media_context = _build_recent_media_batch_context(conversation, msg_doc)
            if not media_context:
                if content_type in TRANSCRIPT_CONTENT_TYPES:
                    media_context = build_transcript_context_for_chat(media_url, content_type, body_text)
                else:
                    media_context = build_media_context_for_chat(media_url, content_type, body_text)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Media Context Failed")
            media_context = f"Customer sent a {content_type} attachment."
    elif _looks_like_recent_attachment_followup(body_text):
        try:
            media_context = _build_recent_attachment_followup_context(conversation, msg_doc)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Recent Attachment Context Failed")
            media_context = ""

    last_user_query = _meaningful_body(body_text, content_type) or media_context[:500]

    system_prompt = build_system_prompt_from_config(prompt_config)
    if not system_prompt.strip():
        frappe.log_error(
            "Autopilot skipped: System Prompt is empty in WA Chat Hub Settings "
            f"(channel account: {channel_account or 'global'}).",
            "WA AI Autopilot Config",
        )
        return

    known_context = _build_known_conversation_context(conversation)
    if known_context:
        system_prompt = f"{system_prompt}\n\n{known_context}"

    mcp_context = _build_mcp_operating_context(conversation, channel_account)
    if mcp_context:
        system_prompt = f"{system_prompt}\n\n{mcp_context}"

    if media_context:
        system_prompt = f"{system_prompt}\n\n{media_context}"

    kb_result_count = 0
    if last_user_query:
        try:
            kb_results = search_knowledge_base(
                last_user_query,
                top_k=3,
                department=department,
                channel_account=channel_account,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Knowledge Search Failed")
            kb_results = []
        kb_result_count = len(kb_results)
        if kb_results:
            kb_blocks = [
                f"--- {kb['title']} ---\n{kb['content']}"
                for kb in kb_results
                if kb.get("content")
            ]
            if kb_blocks:
                system_prompt = f"{system_prompt}\n\n" + "\n\n".join(kb_blocks)

    multilingual_policy = get_multilingual_policy(prompt_config, settings)
    if multilingual_policy:
        system_prompt = f"{system_prompt}\n\n{multilingual_policy}"

    if media_url and content_type in MEDIA_CONTENT_TYPES:
        media_fallback_reply = _media_fallback_reply(
            content_type,
            media_context,
            prompt_config=prompt_config,
            system_prompt=system_prompt,
        )

    latest_user_text = _build_latest_user_turn(msg_doc, media_context, use_vision_for_image)

    current_inbound = None
    if use_vision_for_image:
        current_inbound = {
            "media_url": media_url,
            "prompt": latest_user_text or media_context or "",
        }

    _log_ai_timing(
        "context_ready",
        message=message_id,
        conversation=conversation,
        duration_sec=elapsed(context_started),
        history_count=len(history_before_current),
        kb_results=kb_result_count,
        has_media=1 if media_context else 0,
        vision=1 if use_vision_for_image else 0,
    )

    providers = _load_providers()
    if not providers:
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="no_active_provider",
            total_sec=elapsed(total_started),
        )
        frappe.log_error("No active WA LLM Providers found.", "WA AI Bot Error")
        return

    for provider in providers:
        provider_started = time.monotonic()
        _log_ai_timing(
            "provider_start",
            message=message_id,
            conversation=conversation,
            provider=provider.name,
            provider_type=provider.provider_type,
            model=provider.model_name,
        )
        try:
            _reset_current_reply_tool_state()
            response_text = call_provider(
                provider,
                system_prompt,
                history_before_current,
                latest_user_text=latest_user_text,
                current_inbound=current_inbound,
            )
            if not response_text or not response_text.strip():
                _log_ai_timing(
                    "provider_empty",
                    message=message_id,
                    conversation=conversation,
                    provider=provider.name,
                    model=provider.model_name,
                    duration_sec=elapsed(provider_started),
                )
                continue

            _log_ai_timing(
                "provider_done",
                message=message_id,
                conversation=conversation,
                provider=provider.name,
                model=provider.model_name,
                duration_sec=elapsed(provider_started),
            )
            log_agent_event(
                "Provider",
                "Succeeded",
                conversation=conversation,
                message=str(message_id),
                provider=provider.name,
                provider_type=provider.provider_type,
                model_name=provider.model_name,
                duration_sec=elapsed(provider_started),
            )
            response_text = _polish_autopilot_reply(response_text.strip())
            if _looks_like_degenerate_reply(response_text):
                _log_ai_timing(
                    "provider_bad_output",
                    message=message_id,
                    conversation=conversation,
                    provider=provider.name,
                    model=provider.model_name,
                    reason="degenerate_repetition",
                    duration_sec=elapsed(provider_started),
                )
                _safe_log_error(
                    "WA AI Provider Bad Output",
                    f"Rejected repetitive provider reply for message {message_id}: {response_text[:500]}",
                )
                continue

            if is_shipkia_channel:
                guarded_response, rate_guard_blocked = _guard_unverified_rate_reply(
                    response_text,
                    latest_user_text=latest_user_text,
                )
                if rate_guard_blocked:
                    _log_ai_timing(
                        "rate_guard_blocked",
                        message=message_id,
                        conversation=conversation,
                        provider=provider.name,
                        model=provider.model_name,
                    )
                    response_text = guarded_response

            delivered_mode = _deliver_or_draft_ai_reply(conversation, response_text, settings, message_id)
            if not delivered_mode:
                _log_ai_timing(
                    "skip",
                    message=message_id,
                    conversation=conversation,
                    reason="near_duplicate_reply",
                    total_sec=elapsed(total_started),
                )
                return

            _log_ai_timing(
                "total_done",
                message=message_id,
                conversation=conversation,
                mode=delivered_mode,
                total_sec=elapsed(total_started),
            )
            return
        except Exception as e:
            log_agent_event(
                "Provider",
                "Failed",
                conversation=conversation,
                message=str(message_id),
                provider=provider.name,
                provider_type=provider.provider_type,
                model_name=provider.model_name,
                duration_sec=elapsed(provider_started),
                error_message=str(e)[:500],
                traceback=frappe.get_traceback(),
            )
            _log_ai_timing(
                "provider_failed",
                message=message_id,
                conversation=conversation,
                provider=provider.name,
                model=provider.model_name,
                duration_sec=elapsed(provider_started),
                error=str(e)[:140],
            )
            _safe_log_error(
                "WA AI Fallback Warning",
                f"LLM Provider {provider.name} failed: {str(e)}",
            )
            continue

    if media_fallback_reply:
        delivered_mode = _deliver_or_draft_ai_reply(conversation, media_fallback_reply, settings, message_id)
        mode = f"fallback_{delivered_mode}" if delivered_mode else "fallback_duplicate_skip"
        _log_ai_timing(
            "total_done",
            message=message_id,
            conversation=conversation,
            mode=f"media_{mode}",
            total_sec=elapsed(total_started),
        )
        return

    if is_shipkia_channel:
        shipkia_fallback_reply = _shipkia_render_reply_template(
            "provider_fallback",
            conversation,
            {},
        )
        if shipkia_fallback_reply:
            delivered_mode = _deliver_or_draft_ai_reply(conversation, shipkia_fallback_reply, settings, message_id)
            mode = f"fallback_{delivered_mode}" if delivered_mode else "fallback_duplicate_skip"
            _log_ai_timing(
                "total_done",
                message=message_id,
                conversation=conversation,
                mode=f"shipkia_{mode}",
                total_sec=elapsed(total_started),
            )
            return

    text_fallback_reply = _text_provider_fallback_reply(body_text, content_type)
    if text_fallback_reply:
        delivered_mode = _deliver_or_draft_ai_reply(conversation, text_fallback_reply, settings, message_id)
        mode = f"fallback_{delivered_mode}" if delivered_mode else "fallback_duplicate_skip"
        _log_ai_timing(
            "total_done",
            message=message_id,
            conversation=conversation,
            mode=f"text_{mode}",
            total_sec=elapsed(total_started),
        )
        return

    _safe_log_error(
        "WA AI Fatal Error",
        f"All LLM Providers failed for conversation {conversation}.",
    )
    _log_ai_timing(
        "total_failed",
        message=message_id,
        conversation=conversation,
        total_sec=elapsed(total_started),
    )


def _should_auto_send(settings) -> bool:
    return (settings.autopilot_mode or "") == "Limited Auto Reply"


def _deliver_or_draft_ai_reply(
    conversation: str,
    response_text: str,
    settings,
    message_id: str | None = None,
) -> str:
    response_text = (response_text or "").strip()
    if not response_text:
        return ""
    if _looks_like_recent_duplicate_reply(conversation, response_text, message_id=message_id):
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="near_duplicate_reply",
        )
        return ""
    if _should_auto_send(settings):
        _deliver_ai_reply(conversation, response_text)
        return "auto_send"
    create_ai_suggestion(conversation, "Reply Draft", response_text)
    frappe.db.commit()
    return "draft"


def _normalize_reply_for_similarity(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip().lower())
    text = re.sub(r"[^\w\s\u0900-\u097F]", "", text)
    return text.strip()


def _near_duplicate_text(a: str, b: str) -> bool:
    left = _normalize_reply_for_similarity(a)
    right = _normalize_reply_for_similarity(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 35:
        return False
    if left in right or right in left:
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.92


def _looks_like_recent_duplicate_reply(
    conversation: str,
    response_text: str,
    message_id: str | None = None,
) -> bool:
    filters = {
        "conversation": conversation,
        "direction": "Outbound",
        "sender_type": ["in", ["AI", "Agent"]],
        "delivery_status": ["!=", "Failed"],
    }
    if message_id:
        inbound_creation = safe_ai_get_value("Chat Message", message_id, "creation")
        if inbound_creation:
            filters["creation"] = [">", inbound_creation]
    rows = safe_ai_get_all(
        "Chat Message",
        filters=filters,
        fields=["name", "body", "sender_type"],
        order_by="creation desc, name desc",
        limit=3,
    )
    for row in rows:
        if _near_duplicate_text(response_text, row.get("body") or ""):
            _log_ai_timing(
                "duplicate_reply_detected",
                conversation=conversation,
                prior_message=row.get("name"),
                prior_sender=row.get("sender_type"),
            )
            return True
    return False


def _build_known_conversation_context(conversation: str) -> str:
    fields = [
        "channel_account",
        "company",
        "contact",
        "department",
        "assigned_to",
        "linked_crm_lead",
        "linked_reference_doctype",
        "linked_reference_name",
        "lead_score",
        "lead_lan",
        "lead_temperature",
        "ai_summary",
    ]
    convo = safe_ai_get_value("Chat Conversation", conversation, fields, as_dict=True) or {}
    facts = []
    if convo.get("channel_account"):
        facts.append(f"Channel account: {convo.get('channel_account')}")
    if convo.get("company"):
        facts.append(f"Company: {convo.get('company')}")
    contact_name = convo.get("contact")
    if contact_name:
        contact = safe_ai_get_value(
            "Chat Contact",
            contact_name,
            ["display_name", "phone_number", "linked_lead"],
            as_dict=True,
        ) or {}
        if contact.get("display_name"):
            facts.append(f"Contact name: {contact.get('display_name')}")
        if contact.get("phone_number"):
            facts.append(f"WhatsApp phone: {contact.get('phone_number')}")
        if contact.get("linked_lead"):
            facts.append(f"Linked Lead: {contact.get('linked_lead')}")

    for label, fieldname in (
        ("Department", "department"),
        ("Assigned to", "assigned_to"),
        ("CRM Lead", "linked_crm_lead"),
        ("Linked reference type", "linked_reference_doctype"),
        ("Linked reference name", "linked_reference_name"),
        ("Lead score", "lead_score"),
        ("Lead language", "lead_lan"),
        ("Lead temperature", "lead_temperature"),
    ):
        value = convo.get(fieldname)
        if value not in (None, ""):
            facts.append(f"{label}: {value}")

    if convo.get("ai_summary"):
        facts.append("Existing conversation summary: " + _truncate_history_text(convo.get("ai_summary"), 1000))

    if not facts:
        return ""
    return (
        "Known conversation facts. Use these as already-known details and do not ask for them again unless "
        "the customer changes or corrects them:\n- "
        + "\n- ".join(str(fact) for fact in facts if fact)
    )


def _build_mcp_operating_context(conversation: str, channel_account: str | None) -> str:
    company = safe_ai_get_value("Chat Channel Account", channel_account, "company") if channel_account else ""
    contact_phone = ""
    convo = safe_ai_get_value("Chat Conversation", conversation, ["contact"], as_dict=True) or {}
    if convo.get("contact"):
        contact_phone = safe_ai_get_value("Chat Contact", convo.get("contact"), "phone_number") or ""

    phone_digits = re.sub(r"\D", "", str(contact_phone or ""))
    phone_variants = []
    if phone_digits:
        phone_variants.append(phone_digits)
        if phone_digits.startswith("91") and len(phone_digits) == 12:
            phone_variants.extend([phone_digits[-10:], f"+91{phone_digits[-10:]}"])
        elif len(phone_digits) == 10:
            phone_variants.extend([f"91{phone_digits}", f"+91{phone_digits}"])
    phone_variants = list(dict.fromkeys(phone_variants))

    lines = [
        "MCP operating rule:",
        "- For any ShipKia customer-specific data question, use the configured company MCP tools before answering when tools are available.",
        "- Customer-specific data includes Lead details, business profile, aggregator status, shipping requirements, pickup/delivery details, rate discussions, callback notes, payment mode, RTO, and shipment/order records stored in ERP.",
        "- Do not ask for customer name before using the known WhatsApp phone for Lead lookup.",
        "- First search Lead by WhatsApp phone using or_filters across phone fields. If no Lead is found, then search CRM Lead for already-qualified customers.",
        "- Phone lookup fields to try with or_filters: mobile, phone, mobile_no, phone_number, whatsapp_number, custom_whatsapp_number.",
        "- Do not use only one field such as phone. Many records store WhatsApp numbers in mobile.",
        "- Reply only from MCP results for stored data. If a field is missing in MCP result, say that detail is not visible and offer a ShipKia team callback if needed.",
        "- For exact rates, do not invent courier-wise or final prices. Offer a callback from the ShipKia team.",
    ]
    if company:
        lines.append(f"- Company for MCP selection: {company}.")
    if channel_account:
        lines.append(f"- Channel account: {channel_account}.")
    if phone_variants:
        lines.append("- WhatsApp phone variants to use: " + ", ".join(phone_variants) + ".")
    return "\n".join(lines)


def _channel_prefers_mcp_agent(channel_account: str | None, settings=None) -> bool:
    if not channel_account or not getattr(settings, "allow_mcp_access", 0):
        return False
    if not safe_ai_exists("DocType", "WA Channel Account Prompt Map"):
        return False
    try:
        rows = safe_ai_get_all(
            "WA Channel Account Prompt Map",
            filters={"chat_channel_account": channel_account, "is_active": 1},
            fields=["name", "system_prompt"],
            limit=1,
        )
    except Exception:
        rows = []
    if not rows:
        return False
    prompt = str((rows[0] or {}).get("system_prompt") or "").lower()
    return "mcp" in prompt or "tool" in prompt


def _quick_media_only_reply(content_type: str, body: str, media_url: str) -> str:
    content_type = (content_type or "Text").title()
    if content_type != "Document" or not (media_url or "").strip():
        return ""
    if _meaningful_body(body, content_type) and not _generic_document_caption(body):
        return ""
    return (
        "Document mil gaya. Agar ye rate card, shipment sheet, invoice ya order details hain, ShipKia team ise review kar legi. "
        "Aap apna business/store name, monthly shipments aur current aggregator bhi share kar dijiye."
    )


def _generic_document_caption(body: str) -> bool:
    normalized = (body or "").strip().lower()
    return normalized in {
        "document",
        "doc",
        "file",
        "invoice",
        "rate card",
        "ratecard",
        "orders",
        "order sheet",
        "shipment sheet",
        "shipping details",
        "attached",
        "attachment",
        "ye document hai",
        "ye file hai",
    }


def _text_provider_fallback_reply(body: str, content_type: str) -> str:
    if (content_type or "Text").title() != "Text":
        return ""

    text = (body or "").strip().lower()
    if not text:
        return ""

    greeting_only = {
        "hi",
        "hello",
        "hey",
        "hii",
        "helo",
        "namaste",
        "namaskar",
        "good morning",
        "good afternoon",
        "good evening",
        "can we talk",
    }
    if text in greeting_only:
        return (
            "Hi, welcome to ShipKia. Shipping rates ya onboarding ke liye apna business/store name, "
            "monthly shipments aur current aggregator share kar dijiye."
        )

    rate_keywords = (
        "rate",
        "rates",
        "price",
        "pricing",
        "charges",
        "shipping cost",
        "courier",
        "freight",
    )
    if any(keyword in text for keyword in rate_keywords):
        return (
            "Rates ke liye pickup city, delivery city aur approx weight ek message mein share kar dijiye. "
            "Exact rate PIN code aur serviceability ke hisaab se vary kar sakta hai."
        )

    return (
        "Message mil gaya. ShipKia shipping support ke liye business type, business/store name, "
        "monthly shipments aur current aggregator share kar dijiye."
    )


def _media_fallback_reply(
    content_type: str,
    media_context: str,
    prompt_config=None,
    system_prompt: str = "",
) -> str:
    content_type = (content_type or "").title()
    context = media_context or ""
    lower = context.lower()

    if content_type in ("Image", "Document"):
        extracted = _extract_context_block(context, "Extracted text from attachment:")
        if extracted:
            return (
                "Document/image mil gaya. Isme jo details visible hain unhe ShipKia team review kar legi. "
                "Agar aap rates compare karna chahte hain to pickup city, delivery city, approx weight, "
                "monthly shipments aur current aggregator share kar dijiye."
            )
        return (
            "Document/image mil gaya, lekin details clearly read nahi ho paayi. "
            "Aap pickup/delivery city, approx weight, monthly shipments aur current aggregator text mein share kar dijiye."
        )

    if content_type == "Video":
        if "no usable voice transcript" in lower or "could not be transcribed" in lower:
            return (
                "Video mil gaya hai, lekin voice/text clear nahi hai. "
                "Aap shipping requirement text mein share kar dijiye: pickup city, delivery city, approx weight aur monthly shipments."
            )
        return (
            "Video mil gaya hai. ShipKia team context review kar legi. "
            "Faster help ke liye shipping requirement text mein bhi share kar dijiye."
        )

    if content_type == "Audio":
        transcript = _extract_context_block(context, "Audio transcript:")
        if transcript:
            return _audio_transcript_fallback_reply(transcript)
        return (
            "Audio mil gaya, lekin voice clear transcript nahi ban paayi. "
            "Kripya apni shipping requirement text mein share kar dijiye."
        )

    return ""


def _prompt_backed_audio_fallback(transcript: str, prompt_config, system_prompt: str) -> str:
    transcript = _normalize_audio_transcript_for_reply((transcript or "").strip())
    if not transcript or _looks_like_foreign_audio_hallucination(transcript):
        return ""

    prompt = (system_prompt or "").strip()
    if not prompt and prompt_config:
        prompt = build_system_prompt_from_config(prompt_config)
        multilingual_policy = (getattr(prompt_config, "multilingual_reply_policy", None) or "").strip()
        if multilingual_policy:
            prompt = f"{prompt}\n\n{multilingual_policy}"
    if not prompt:
        return ""

    fallback_instruction = (
        f"{prompt}\n\n"
        "Fallback audio handling instruction:\n"
        "- Use the account prompt rules above as the source of truth.\n"
        "- Reply as a normal WhatsApp chat message, not as a transcript/debug message.\n"
        "- Do not show a heading like 'Audio transcript'.\n"
        "- Handle only ShipKia shipping aggregator sales/support queries.\n"
        "- If the transcript asks for rates, follow ShipKia rules: starting rates only, exact/final rates via callback.\n"
        "- If details are missing, ask compactly for business/store name, monthly shipments, current aggregator, pickup city, delivery city and weight as needed.\n"
        "- If the transcript is unclear, ask for the shipping requirement again in text.\n"
        "- Keep reply in the prompt default style: Hinglish/Roman Hindi unless transcript clearly requires another supported language.\n"
        "- If the transcript seems hallucinated/foreign/unusable, ask for a clear Hinglish/Roman Hindi voice note or text.\n"
    )
    user_text = f"Customer audio transcript:\n{transcript[:1000]}"

    for provider in _load_providers():
        try:
            response = call_provider(
                provider,
                fallback_instruction,
                [],
                latest_user_text=user_text,
                current_inbound=None,
            )
            response = _polish_autopilot_reply((response or "").strip())
            if response:
                return response
        except Exception as exc:
            _safe_log_error(
                "WA AI Prompt-backed Audio Fallback Failed",
                f"Provider {getattr(provider, 'name', '')} failed: {exc}",
            )
            continue
    return ""


def _audio_transcript_fallback_reply(transcript: str) -> str:
    text = _normalize_audio_transcript_for_reply((transcript or "").strip())
    lower = text.lower()
    if _looks_like_foreign_audio_hallucination(text):
        return (
            "Audio mil gaya, lekin voice clear samajh nahi aa paayi. "
            "Kripya apni shipping requirement ek baar text mein share kar dijiye."
        )

    if re.search(r"\b(rate|rates|price|pricing|charge|charges|courier|freight)\b", lower):
        return (
            "Audio mil gaya. Rates ke liye pickup city, delivery city aur approx weight text mein share kar dijiye. "
            "ShipKia starting rates bata dega; exact rate ke liye team callback arrange kar sakti hai."
        )

    if re.search(r"\b(aggregator|shiprocket|shipmozo|nimbus|ithink|delhivery|xpressbees|ecom|shadowfax)\b", lower):
        return (
            "Audio mil gaya. Aap current aggregator aur shipping setup ke baare mein bata rahe hain. "
            "Business/store name, monthly shipments, current rate aur RTO percentage text mein share kar dijiye."
        )

    if re.search(r"\b(delivery|deliver|shipping|shipment|ship|courier|pickup|cod|prepaid)\b", lower):
        return (
            "Audio mil gaya. Shipping setup ke liye pickup city, delivery city, package weight, "
            "payment mode aur monthly shipment volume text mein share kar dijiye."
        )

    return (
        f"Ji, aap shayad yeh kehna chahte hain: \"{_clean_audio_text_for_chat(text)[:160]}\". "
        "Kripya ShipKia ke liye apni shipping requirement text mein share kar dijiye."
    )


def _normalize_audio_transcript_for_reply(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if _contains_arabic_script(cleaned):
        return "audio clear nahi hai"
    return cleaned


def _contains_arabic_script(text: str) -> bool:
    return any("\u0600" <= ch <= "\u06ff" for ch in (text or ""))


def _looks_like_foreign_audio_hallucination(text: str) -> bool:
    lower = (text or "").strip().lower()
    if not lower:
        return True
    foreign_fragments = (
        "o que",
        "mão",
        "coisa",
        "não",
        "né",
        "mais que",
        "possible topics",
    )
    return any(fragment in lower for fragment in foreign_fragments)


def _clean_audio_text_for_chat(text: str) -> str:
    cleaned = _normalize_audio_transcript_for_reply((text or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _extract_context_block(context: str, marker: str) -> str:
    if marker not in context:
        return ""
    tail = context.split(marker, 1)[1].strip()
    if "\nUse " in tail:
        tail = tail.split("\nUse ", 1)[0].strip()
    return tail


def _looks_like_recent_attachment_followup(body: str) -> bool:
    text = (body or "").strip().lower()
    if not text:
        return False
    keywords = (
        "document",
        "doc",
        "file",
        "attachment",
        "invoice",
        "rate card",
        "order sheet",
        "shipment sheet",
        "image",
        "photo",
        "pic",
        "value",
        "values",
        "details",
        "kya h",
        "kya hai",
        "btaoge",
        "bataoge",
        "explain",
        "read",
        "padh",
    )
    return any(keyword in text for keyword in keywords)


def _build_recent_attachment_followup_context(conversation: str, msg_doc) -> str:
    current_creation = getattr(msg_doc, "creation", None)
    filters = {
        "conversation": conversation,
        "direction": "Inbound",
        "content_type": ["in", ["Image", "Document"]],
        "media_url": ["is", "set"],
    }
    if current_creation:
        filters["creation"] = ["<", current_creation]

    recent = safe_ai_get_all(
        "Chat Message",
        filters=filters,
        fields=["name", "creation", "content_type", "body", "media_url"],
        order_by="creation desc, name desc",
        limit=1,
    )
    if not recent:
        return ""

    row = recent[0]
    context = build_media_context_for_chat(
        row.media_url,
        row.content_type,
        str(row.body or ""),
    )
    if not context:
        return ""
    return (
        "Customer is asking a follow-up question about the most recent ShipKia image/document attachment. "
        "Use the extracted text below only for shipping, lead, rate, invoice, order, pickup/delivery, "
        "aggregator, RTO, or callback context. Do not infer exact rates from attachments unless the "
        "ShipKia rate rules allow it; offer callback for exact/final pricing.\n\n"
        f"Recent attachment: Chat Message {row.name} sent at {row.creation}\n"
        f"{context}"
    )


def _build_recent_media_batch_context(conversation: str, msg_doc) -> str:
    current_creation = getattr(msg_doc, "creation", None)
    if not conversation or not current_creation:
        return ""

    if _is_media_message(msg_doc):
        batch_start = _media_burst_window_start(conversation, current_creation)
        start_operator = ">="
    else:
        batch_start = _autopilot_batch_window_start(conversation, current_creation)
        start_operator = ">"
    filters = [
        ["conversation", "=", conversation],
        ["direction", "=", "Inbound"],
        ["content_type", "in", list(MEDIA_CONTENT_TYPES)],
        ["media_url", "is", "set"],
        ["creation", "<=", current_creation],
        ["creation", start_operator, batch_start],
    ]

    rows = safe_ai_get_all(
        "Chat Message",
        filters=filters,
        fields=["name", "creation", "content_type", "body", "media_url"],
        order_by="creation asc, name asc",
        limit=8,
    )
    if not rows:
        return ""

    blocks = []
    for index, row in enumerate(rows, start=1):
        row_content_type = str(row.content_type or "Text").title()
        row_body = str(row.body or "")
        try:
            if row_content_type in TRANSCRIPT_CONTENT_TYPES:
                context = build_transcript_context_for_chat(row.media_url, row_content_type, row_body)
            else:
                context = build_media_context_for_chat(row.media_url, row_content_type, row_body)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Media Batch Context Item Failed")
            context = f"Customer sent a {row_content_type} attachment."
        if context:
            blocks.append(
                f"Attachment {index}: Chat Message {row.name}, type {row_content_type}, sent at {row.creation}\n{context}"
            )

    if not blocks:
        return ""

    if len(blocks) == 1:
        return blocks[0]

    return (
        f"Customer sent {len(blocks)} recent media attachments before this reply. "
        "Review them together and respond once for the combined customer action.\n\n"
        + "\n\n".join(blocks)
    )


def _autopilot_batch_lock_name(conversation: str) -> str:
    digest = hashlib.sha256((conversation or "unknown").encode("utf-8")).hexdigest()[:24]
    return f"wa_ai_batch_{digest}"


def _autopilot_batch_window_start(conversation: str, current_creation):
    current_dt = get_datetime(current_creation)
    recent_start = add_to_date(current_dt, minutes=-AUTOPILOT_BATCH_LOOKBACK_MINUTES)
    last_outbound = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Outbound",
            "creation": ["<", current_dt],
        },
        fields=["creation"],
        order_by="creation desc, name desc",
        limit=1,
    )
    if not last_outbound:
        return recent_start

    last_outbound_dt = get_datetime(last_outbound[0].creation)
    if last_outbound_dt:
        return max(recent_start, last_outbound_dt)
    return recent_start


def _shipkia_conversation_channel_account(conversation: str | None) -> str:
    if not conversation:
        return ""
    return str(safe_ai_get_value("Chat Conversation", conversation, "channel_account") or "")


def _shipkia_combined_inbound_since_last_outbound(
    conversation: str | None,
    current_creation=None,
) -> str:
    if not conversation:
        return ""

    current_dt = get_datetime(current_creation) if current_creation else None
    last_outbound_filters: dict[str, Any] = {
        "conversation": conversation,
        "direction": "Outbound",
    }
    if current_dt:
        last_outbound_filters["creation"] = ["<", current_dt]
    last_outbound = safe_ai_get_all(
        "Chat Message",
        filters=last_outbound_filters,
        fields=["creation"],
        order_by="creation desc, name desc",
        limit=1,
    )

    filters: list[list[Any]] = [
        ["conversation", "=", conversation],
        ["direction", "=", "Inbound"],
        ["content_type", "=", "Text"],
    ]
    if last_outbound:
        filters.append(["creation", ">", last_outbound[0].creation])
    if current_dt:
        filters.append(["creation", "<=", current_dt])

    rows = safe_ai_get_all(
        "Chat Message",
        filters=filters,
        fields=["name", "body", "creation", "sender_type"],
        order_by="creation asc, name asc",
        limit=12,
    )
    bodies = [
        str(row.body or "").strip()
        for row in rows or []
        if str(row.sender_type or "").strip() not in ("AI", "System", "Bot")
        and str(row.body or "").strip()
    ]
    return "\n".join(bodies).strip()


def _shipkia_should_extend_fragment_wait(conversation: str | None, latest_doc) -> bool:
    if not conversation or not latest_doc:
        return False
    if "shipkia" not in _shipkia_conversation_channel_account(conversation).casefold():
        return False
    if _is_media_message(latest_doc):
        return False

    current_creation = _doc_value(latest_doc, "creation", None)
    current_dt = get_datetime(current_creation) if current_creation else None
    last_outbound_filters: dict[str, Any] = {
        "conversation": conversation,
        "direction": "Outbound",
    }
    if current_dt:
        last_outbound_filters["creation"] = ["<", current_dt]
    last_outbound = safe_ai_get_all(
        "Chat Message",
        filters=last_outbound_filters,
        fields=["body", "creation"],
        order_by="creation desc, name desc",
        limit=1,
    )
    if not last_outbound:
        return False

    prompt_kind = _shipkia_prompt_kind(str(last_outbound[0].body or ""))
    if not prompt_kind:
        return False

    combined_text = _shipkia_combined_inbound_since_last_outbound(conversation, current_creation)
    if not combined_text:
        return False

    if prompt_kind == "pickup_delivery":
        details = _extract_shipkia_rate_details(combined_text)
        has_route = bool(
            (details.get("pickup_city") or details.get("pickup_pincode"))
            and (
                details.get("delivery_city")
                or details.get("delivery_pincode")
                or details.get("delivery_scope")
            )
        )
        has_weight = bool(details.get("weight_grams"))
        return has_route != has_weight

    if prompt_kind == "current_rate_rto":
        has_current_rate = _shipkia_parse_current_shipping_rate(combined_text, expected=True) is not None
        has_rto = _shipkia_parse_rto_percentage(combined_text, expected=True) is not None
        return has_current_rate != has_rto

    if prompt_kind in {"compact_profile", "business_name", "business_operation_mode", "current_aggregator_status"}:
        profile = _shipkia_extract_compact_profile(combined_text, expected=True)
        filled_count = sum(
            1
            for fieldname in ("business_name", "monthly_shipments", "current_aggregator_name", "current_aggregator_status")
            if profile.get(fieldname) not in (None, "")
        )
        return 0 < filled_count < 2

    return False


def _autopilot_batch_already_answered(conversation: str, msg_doc) -> bool:
    current_creation = getattr(msg_doc, "creation", None)
    if not conversation or not current_creation:
        return False

    if _is_media_message(msg_doc) and _media_burst_already_answered(conversation, msg_doc):
        return True

    batch_start = _autopilot_batch_window_start(conversation, current_creation)
    outbound_rows = safe_ai_get_all(
        "Chat Message",
        filters=[
            ["conversation", "=", conversation],
            ["direction", "=", "Outbound"],
            ["sender_type", "in", ["AI", "Agent"]],
            ["creation", ">", batch_start],
        ],
        fields=["name", "delivery_status", "sender_type", "creation"],
        order_by="creation asc, name asc",
        limit=10,
    )
    for row in outbound_rows:
        if str(row.delivery_status or "") in ("Failed", "Pending"):
            continue
        return True
    return False


def _media_burst_already_answered(conversation: str, msg_doc) -> bool:
    current_creation = getattr(msg_doc, "creation", None)
    if not conversation or not current_creation:
        return False

    current_dt = get_datetime(current_creation)
    burst_start = _media_burst_window_start(conversation, current_creation)
    prior_media = safe_ai_get_all(
        "Chat Message",
        filters=[
            ["conversation", "=", conversation],
            ["direction", "=", "Inbound"],
            ["content_type", "in", list(MEDIA_CONTENT_TYPES)],
            ["media_url", "is", "set"],
            ["creation", ">=", burst_start],
            ["creation", "<", current_dt],
        ],
        fields=["name", "creation"],
        order_by="creation asc, name asc",
        limit=1,
    )
    if not prior_media:
        return False

    outbound_rows = safe_ai_get_all(
        "Chat Message",
        filters=[
            ["conversation", "=", conversation],
            ["direction", "=", "Outbound"],
            ["sender_type", "in", ["AI", "Agent"]],
            ["creation", ">", prior_media[0].creation],
            ["creation", "<=", current_dt],
        ],
        fields=["name", "delivery_status", "sender_type", "creation"],
        order_by="creation asc, name asc",
        limit=10,
    )
    for row in outbound_rows:
        if str(row.delivery_status or "") in ("Failed", "Pending"):
            continue
        return True
    return False


def _media_burst_window_start(conversation: str, current_creation):
    current_dt = get_datetime(current_creation)
    lookback_start = add_to_date(current_dt, minutes=-AUTOPILOT_MEDIA_BURST_LOOKBACK_MINUTES)
    rows = safe_ai_get_all(
        "Chat Message",
        filters=[
            ["conversation", "=", conversation],
            ["direction", "=", "Inbound"],
            ["content_type", "in", list(MEDIA_CONTENT_TYPES)],
            ["media_url", "is", "set"],
            ["creation", ">=", lookback_start],
            ["creation", "<=", current_dt],
        ],
        fields=["name", "creation"],
        order_by="creation desc, name desc",
        limit=20,
    )

    burst_start = current_dt
    previous_dt = current_dt
    for row in rows:
        row_dt = get_datetime(row.creation)
        if not row_dt or not previous_dt:
            continue
        if (previous_dt - row_dt).total_seconds() > AUTOPILOT_MEDIA_BURST_GAP_SECONDS:
            break
        burst_start = row_dt
        previous_dt = row_dt

    last_outbound = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Outbound",
            "creation": ["<", current_dt],
        },
        fields=["creation"],
        order_by="creation desc, name desc",
        limit=1,
    )
    if last_outbound:
        last_outbound_dt = get_datetime(last_outbound[0].creation)
        if last_outbound_dt and burst_start and last_outbound_dt > burst_start:
            return last_outbound_dt

    return burst_start


def _doc_value(doc, fieldname: str, default=None):
    if isinstance(doc, dict):
        return doc.get(fieldname, default)
    return getattr(doc, fieldname, default)


def _is_media_message(doc) -> bool:
    content_type = str(_doc_value(doc, "content_type", "Text") or "Text").title()
    media_url = str(_doc_value(doc, "media_url", None) or "").strip()
    return bool(content_type in MEDIA_CONTENT_TYPES and media_url)


def _autopilot_batching_enabled(settings) -> bool:
    return bool(cint(getattr(settings, "enable_autopilot_reply_batching", 1)))


def _autopilot_reply_delay_seconds(doc, settings) -> int:
    if _is_media_message(doc):
        value = getattr(settings, "media_autopilot_reply_delay_seconds", None)
        default = DEFAULT_MEDIA_AUTOPILOT_REPLY_DELAY_SECONDS
    else:
        value = getattr(settings, "text_autopilot_reply_delay_seconds", None)
        default = DEFAULT_TEXT_AUTOPILOT_REPLY_DELAY_SECONDS

    if value in (None, ""):
        return default
    try:
        return max(0, cint(value))
    except Exception:
        return default


def _wait_for_autopilot_batch_window(msg_doc, settings) -> tuple[bool, int]:
    if not _autopilot_batching_enabled(settings):
        return False, 0

    delay_seconds = _autopilot_reply_delay_seconds(msg_doc, settings)
    if delay_seconds and not getattr(frappe.flags, "in_test", False):
        time.sleep(delay_seconds)

    return _newer_autopilot_message_exists(msg_doc), delay_seconds


def _wait_for_conversation_batch_window(conversation: str, settings):
    latest_doc = _latest_autopilot_inbound(conversation)
    if not latest_doc:
        return None, 0

    if not _autopilot_batching_enabled(settings):
        return latest_doc, 0

    total_wait = 0
    for _ in range(4):
        delay_seconds = _autopilot_reply_delay_seconds(latest_doc, settings)
        if delay_seconds and not getattr(frappe.flags, "in_test", False):
            time.sleep(delay_seconds)
            total_wait += delay_seconds

        refreshed_doc = _latest_autopilot_inbound(conversation)
        if not refreshed_doc:
            return latest_doc, total_wait
        if str(refreshed_doc.name) == str(latest_doc.name):
            if (
                _is_media_message(refreshed_doc)
                and AUTOPILOT_MEDIA_SETTLE_SECONDS
                and not getattr(frappe.flags, "in_test", False)
            ):
                time.sleep(AUTOPILOT_MEDIA_SETTLE_SECONDS)
                total_wait += AUTOPILOT_MEDIA_SETTLE_SECONDS
                settled_doc = _latest_autopilot_inbound(conversation)
                if settled_doc and str(settled_doc.name) != str(refreshed_doc.name):
                    latest_doc = settled_doc
                    continue
            if (
                SHIPKIA_FRAGMENT_GRACE_SECONDS
                and _shipkia_should_extend_fragment_wait(conversation, refreshed_doc)
                and not getattr(frappe.flags, "in_test", False)
            ):
                time.sleep(SHIPKIA_FRAGMENT_GRACE_SECONDS)
                total_wait += SHIPKIA_FRAGMENT_GRACE_SECONDS
                settled_doc = _latest_autopilot_inbound(conversation)
                if settled_doc and str(settled_doc.name) != str(refreshed_doc.name):
                    latest_doc = settled_doc
                    continue
            return refreshed_doc, total_wait
        latest_doc = refreshed_doc

    return latest_doc, total_wait


def _latest_autopilot_inbound(conversation: str):
    if not conversation:
        return None

    rows = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Inbound",
        },
        fields=["name", "body", "content_type", "media_url", "sender_type", "creation"],
        order_by="creation desc, name desc",
        limit=30,
    )
    for row in rows:
        if str(_doc_value(row, "sender_type", "") or "").strip() in ("AI", "System", "Bot"):
            continue
        if _inbound_triggers_autopilot(row):
            return safe_ai_get_doc("Chat Message", row.name)
    return None


def _newer_autopilot_message_exists(msg_doc) -> bool:
    conversation = _doc_value(msg_doc, "conversation")
    current_creation = _doc_value(msg_doc, "creation")
    if not conversation or not current_creation:
        return False

    rows = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Inbound",
            "creation": [">", current_creation],
        },
        fields=["name", "body", "content_type", "media_url", "sender_type"],
        order_by="creation desc, name desc",
        limit=20,
    )
    for row in rows:
        if str(_doc_value(row, "sender_type", "") or "").strip() in ("AI", "System", "Bot"):
            continue
        if _inbound_triggers_autopilot(row):
            return True
    return False


def _inbound_triggers_autopilot(doc) -> bool:
    content_type = str(_doc_value(doc, "content_type", None) or "Text").title()
    if _is_media_message(doc):
        return True
    return bool(_meaningful_body(str(_doc_value(doc, "body", None) or ""), content_type))


def _meaningful_body(body: str, content_type: str = "Text") -> str:
    text = (body or "").strip()
    if not text:
        return ""
    normalized = text.lower()
    if content_type.title() in MEDIA_CONTENT_TYPES and normalized in GENERIC_MEDIA_BODIES:
        return ""
    if normalized in GENERIC_MEDIA_BODIES:
        return ""
    return text


def _load_recent_conversation_history(conversation: str):
    rows = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["name", "direction", "body", "content_type", "media_url"],
        order_by="creation desc, name desc",
        limit=CONVERSATION_HISTORY_LIMIT,
    )
    return list(reversed(rows))


def _format_history_line(row) -> str:
    content_type = str(row.content_type or "Text").title()
    body = _meaningful_body(str(row.body or ""), content_type)
    if body:
        return _truncate_history_text(body)
    if str(row.media_url or "").strip() and content_type in MEDIA_CONTENT_TYPES:
        return f"[sent {content_type}]"
    return ""


def _truncate_history_text(text: str, limit: int = MAX_HISTORY_MESSAGE_CHARS) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return (
        text[:head].rstrip()
        + "\n[Earlier part shortened to keep full chat context within model limits.]\n"
        + text[-tail:].lstrip()
    )


def _safe_log_error(title: str, message: str) -> None:
    try:
        frappe.log_error(title=(title or "")[:140], message=(message or "")[:4000])
    except Exception:
        pass


def _build_latest_user_turn(msg_doc, media_context: str, skip_text: bool) -> str:
    """Always pass the triggering inbound message as the final user turn."""
    if skip_text:
        return ""
    body = _meaningful_body(str(msg_doc.body or ""), str(msg_doc.content_type or "Text").title())
    if body:
        return body
    if media_context:
        return media_context
    return ""


def _shipkia_llm_reply_from_intent(
    reply_intent: str,
    latest_user_text: str,
    conversation: str,
    channel_account: str,
    settings,
    message_id: str | None = None,
) -> str:
    """Let the LLM write ShipKia wording while deterministic code owns workflow state."""
    reply_intent = (reply_intent or "").strip()
    if not reply_intent:
        return ""

    try:
        set_ai_security_context(channel_account=channel_account)
        prompt_config = get_effective_prompt_config(channel_account)
        system_prompt = build_system_prompt_from_config(prompt_config)
        if not system_prompt.strip():
            return ""

        known_context = _build_known_conversation_context(conversation)
        if known_context:
            system_prompt = f"{system_prompt}\n\n{known_context}"

        multilingual_policy = get_multilingual_policy(prompt_config, settings)
        if multilingual_policy:
            system_prompt = f"{system_prompt}\n\n{multilingual_policy}"

        system_prompt = (
            f"{system_prompt}\n\n"
            "ShipKia workflow engine has already chosen the required next action/content below. "
            "Treat it as semantic guidance, not wording to copy. Use your own natural WhatsApp wording according to the system prompt. "
            "Preserve every number, route, rate, payment mode, callback time, and required factual detail exactly. "
            "Do not add extra questions, do not invent rates, and do not mention that a workflow engine or template was used. "
            "Do not quote or paraphrase the customer's latest message back to them. "
            "Maintain one consistent persona: ShipKia's WhatsApp assistant speaks in feminine first-person Hinglish/Hindi "
            "when gendered wording is needed, for example 'kar sakti hoon', 'bata sakti hoon', 'madad kar sakti hoon'. "
            "Never switch to masculine first-person phrases like 'kar sakta hoon' or 'bata sakta hoon'. "
            "For intent-only messages like rate requests, use a natural acknowledgement such as Sure, Bilkul, or Ji instead of repeating the request. "
            "If the latest customer message contains concrete details, acknowledge the detail briefly before asking the required next question. "
            "If recent customer history shows frustration, impatience, profanity, price objection, or negotiation, first acknowledge it briefly and move toward callback/custom pricing support; do not repeat the same rate or the same question. "
            "Do not simply repeat the required reply intent unchanged."
        )

        history = _load_recent_conversation_history(conversation)
        if message_id:
            history = [row for row in history if str(row.name) != str(message_id)]

        latest_turn = (
            "Customer's latest WhatsApp message:\n"
            f"{(latest_user_text or '').strip()}\n\n"
            "Required reply intent/content:\n"
            f"{reply_intent}\n\n"
            "Rewrite requirement: use your own natural wording. Do not echo the customer's sentence. "
            "Acknowledge briefly, then ask/say the required content naturally.\n\n"
            "Return only the final customer-facing WhatsApp reply."
        )

        providers = _load_providers()
        for provider in providers:
            try:
                response_text = call_provider(
                    provider,
                    system_prompt,
                    history,
                    latest_user_text=latest_turn,
                )
                response_text = _polish_autopilot_reply((response_text or "").strip())
                if response_text and not _looks_like_degenerate_reply(response_text):
                    return response_text
            except Exception:
                _safe_log_error(
                    "WA ShipKia LLM Rewrite Failed",
                    f"Provider {getattr(provider, 'name', '')} failed while rewriting ShipKia intent: {frappe.get_traceback()}",
                )
                continue
    except Exception:
        _safe_log_error("WA ShipKia LLM Rewrite Failed", frappe.get_traceback())
    return ""


def _polish_autopilot_reply(text: str) -> str:
    """Strip role prefixes and overly templated openings from outbound text."""
    cleaned = text.strip()
    cleaned = re.sub(
        r"^(Agent|Assistant|AI|Bot|Customer|You)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    boilerplate_starts = (
        "thank you for contacting",
        "thanks for contacting",
        "thank you for reaching out",
        "namaste! thank you",
    )
    lower = cleaned.lower()
    for prefix in boilerplate_starts:
        if lower.startswith(prefix):
            parts = cleaned.split("\n\n", 1)
            if len(parts) > 1 and len(parts[1]) > 20:
                cleaned = parts[1].strip()
            break
    return cleaned


def _looks_like_degenerate_reply(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return True

    words = re.findall(r"[\wऀ-ॿ]+", cleaned.lower())
    if len(words) < 8:
        return False

    token_counts = {}
    for word in words:
        token_counts[word] = token_counts.get(word, 0) + 1
    most_common_count = max(token_counts.values()) if token_counts else 0
    if most_common_count >= 12 and most_common_count / max(len(words), 1) >= 0.35:
        return True

    for size in (2, 3):
        if len(words) < size * 6:
            continue
        phrases = [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]
        phrase_counts = {}
        for phrase in phrases:
            phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1
        if max(phrase_counts.values(), default=0) >= 6:
            return True

    return False


def _conversation_allows_autopilot(conversation: str) -> bool:
    status = safe_ai_get_value("Chat Conversation", conversation, "status")
    return status in (None, "", "Open")


def _already_replied_to_inbound(conversation: str, inbound_message_id: str) -> bool:
    """Only skip duplicate work for the same inbound message, not the whole conversation."""
    inbound_creation = safe_ai_get_value("Chat Message", inbound_message_id, "creation")
    if not inbound_creation:
        return False

    # Raw SQL is used for this duplicate-reply lookup, so gate it explicitly.
    assert_ai_doctype_permission("Chat Message", "read")
    prior_ai = frappe.db.sql(
        """
        SELECT delivery_status, raw_transport_payload
        FROM `tabChat Message`
        WHERE conversation = %s
          AND direction = 'Outbound'
          AND sender_type = 'AI'
          AND creation > %s
        ORDER BY creation ASC
        LIMIT 10
        """,
        (conversation, inbound_creation),
        as_dict=True,
    )
    for row in prior_ai:
        try:
            payload = frappe.parse_json(row.raw_transport_payload) if row.raw_transport_payload else {}
        except Exception:
            payload = {}
        if (payload or {}).get("reply_to_message") != inbound_message_id:
            continue
        if (row.delivery_status or "") in ("Failed", "Pending"):
            return False
        return True
    return False


def _load_providers():
    rows = get_active_llm_provider_rows(CHAT_CAPABILITY)
    providers = []
    for row in rows:
        doc = safe_ai_get_doc("WA LLM Provider", row.name)
        api_key = doc.get_password("api_key")
        if not api_key:
            continue
        providers.append(
            SimpleNamespace(
                name=row.name,
                provider_type=row.provider_type,
                model_name=row.model_name,
                base_url=row.base_url,
                api_key=api_key,
            )
        )
    return providers


def _deliver_ai_reply(conversation: str, response_text: str) -> None:
    send_started = time.monotonic()
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    phone_number = safe_ai_get_value("Chat Contact", convo.contact, "phone_number")
    reply_to_message = (
        getattr(frappe.local, "wa_ai_reply_to_message", None)
        or getattr(frappe.flags, "wa_ai_reply_to_message", None)
    )

    delivery_status = "Sent"
    channel_message_id = None
    outbound = {}

    try:
        _log_ai_timing("send_start", conversation=conversation, channel_account=convo.channel_account)
        outbound = send_outbound_message(conversation, response_text, "Text")
        delivery_status = outbound.get("delivery_status") or "Sent"
        channel_message_id = outbound.get("provider_message_id")
    except Exception:
        log_agent_event(
            "Send",
            "Failed",
            conversation=conversation,
            channel_account=convo.channel_account,
            duration_sec=elapsed(send_started),
            error_message="WA AI autopilot send failed",
            traceback=frappe.get_traceback(),
        )
        frappe.log_error(frappe.get_traceback(), "WA AI Autopilot Send Failed")
        delivery_status = "Failed"
        outbound = {"sent": False, "error": "Interakt send failed"}

    frappe.flags.wa_ai_outbound_reply = True
    frappe.local.wa_ai_outbound_reply = True
    append_result = {}
    try:
        append_result = append_message(
            {
                "channel_account": convo.channel_account,
                "phone_number": phone_number,
                "direction": "Outbound",
                "sender_type": "AI",
                "content_type": "Text",
                "body": response_text,
                "delivery_status": delivery_status,
                "channel_message_id": channel_message_id,
                "raw_transport_payload": {
                    **outbound,
                    "source": "ai_autopilot",
                    "reply_to_message": reply_to_message,
                },
            }
        )
    finally:
        frappe.flags.wa_ai_outbound_reply = False
        frappe.local.wa_ai_outbound_reply = False
    frappe.db.commit()
    log_agent_event(
        "Send",
        "Succeeded" if delivery_status != "Failed" else "Failed",
        conversation=conversation,
        channel_account=convo.channel_account,
        message=str(append_result.get("message") or ""),
        duration_sec=elapsed(send_started),
        response=outbound,
        error_message=str(outbound.get("error")) if isinstance(outbound, dict) and outbound.get("error") is not None else None,
    )
    _log_ai_timing(
        "send_done",
        conversation=conversation,
        delivery_status=delivery_status,
        duration_sec=elapsed(send_started),
    )


def call_provider(provider, system_prompt, history, latest_user_text=None, current_inbound=None):
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        role = "user" if h.direction == "Inbound" else "assistant"
        line = _format_history_line(h)
        if line:
            messages.append({"role": role, "content": line})

    if current_inbound and current_inbound.get("media_url"):
        vision_text = (current_inbound.get("prompt") or "").strip()
        user_content = [{"type": "image_url", "image_url": {"url": current_inbound["media_url"]}}]
        if vision_text:
            user_content.insert(0, {"type": "text", "text": vision_text})
        messages.append({"role": "user", "content": user_content})
    elif latest_user_text:
        messages.append({"role": "user", "content": latest_user_text})

    is_buopso_vllm = "vllm.buopso.net" in str(provider.base_url or "").lower()
    if provider.provider_type in ("OpenAI", "Custom"):
        return call_openai_format(provider, messages, timeout=15 if is_buopso_vllm else 45)
    if provider.provider_type == "Gemini":
        if not provider.base_url:
            provider.base_url = (
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            )
        return call_openai_format(provider, messages, timeout=45)
    if provider.provider_type == "Anthropic":
        raise Exception(
            "Anthropic specific MCP format requires SDK. Please use OpenAI/Gemini/Custom."
        )

    raise Exception(f"Unsupported provider type {provider.provider_type}")


def _reset_current_reply_tool_state() -> None:
    frappe.local.wa_ai_tools_used_current_reply = []


def _record_current_reply_tool_use(tool_name: str) -> None:
    if not tool_name:
        return
    used = getattr(frappe.local, "wa_ai_tools_used_current_reply", None)
    if not isinstance(used, list):
        used = []
    used.append(tool_name)
    frappe.local.wa_ai_tools_used_current_reply = used


def _current_reply_used_tool(tool_name: str) -> bool:
    used = getattr(frappe.local, "wa_ai_tools_used_current_reply", None)
    if not isinstance(used, list):
        return False
    return tool_name in {str(item) for item in used}


def _contains_currency_rate(text: str) -> bool:
    value = text or ""
    if not value:
        return False
    if re.search(r"(?:₹|rs\.?|inr|rupees?)\s*\d", value, flags=re.IGNORECASE):
        return True
    return bool(re.search(r"\d+(?:\.\d+)?\s*(?:₹|rs\.?|inr|rupees?)", value, flags=re.IGNORECASE))


def _guard_unverified_rate_reply(response_text: str, latest_user_text: str | None = None) -> tuple[str, bool]:
    """Block WA autopilot from sending rupee rates unless the calculator ran now."""
    if not _contains_currency_rate(response_text):
        return response_text, False
    if _current_reply_used_tool(SHIPKIA_RATE_TOOL_NAME):
        return response_text, False

    blocked_reply = (
        "Exact rate pickup/delivery PIN aur serviceability ke hisaab se confirm hota hai. "
        "Kya main ShipKia team se callback arrange kar du?"
    )
    return blocked_reply, True


def fetch_mcp_tools():
    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    if not getattr(settings, "allow_mcp_access", 0):
        return []
    if not safe_ai_exists("DocType", "WA MCP Tool Endpoint"):
        return []

    tools_docs = safe_ai_get_all(
        "WA MCP Tool Endpoint",
        filters={"is_active": 1},
        fields=["tool_name", "description", "parameters_schema", "endpoint_url", "http_method", "server", "company"],
    )
    tools = []
    for t in tools_docs:
        try:
            params = (
                json.loads(t.parameters_schema)
                if t.parameters_schema
                else {"type": "object", "properties": {}}
            )
        except Exception:
            params = {"type": "object", "properties": {}}

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.tool_name,
                    "description": t.description or "No description",
                    "parameters": params,
                },
                "_meta": {
                    "url": t.endpoint_url,
                    "method": t.http_method,
                    "server": t.server,
                    "company": t.company,
                },
            }
        )
    return tools


def execute_mcp_tool(tool_name, arguments_dict):
    tools = fetch_mcp_tools()
    tool_meta = next((t["_meta"] for t in tools if t["function"]["name"] == tool_name), None)
    conversation = arguments_dict.get("conversation") if isinstance(arguments_dict, dict) else None
    if not tool_meta:
        log_agent_event(
            "MCP Tool",
            "Failed",
            conversation=conversation,
            tool_name=tool_name,
            request=arguments_dict,
            error_message=f"Tool {tool_name} not found.",
        )
        return f"Error: Tool {tool_name} not found."

    tool_started = time.monotonic()
    try:
        url, headers = _resolve_mcp_http_target(tool_meta)

        if url.startswith("http"):
            resp, response_payload, final_arguments = _request_mcp_http_with_filter_repair(
                url,
                tool_meta["method"],
                arguments_dict,
                headers,
            )
            resp_ok = bool(resp and getattr(resp, "ok", False))
            resp_status = getattr(resp, "status_code", 500) if resp else 500
            resp_text = getattr(resp, "text", str(response_payload)) if resp else str(response_payload)
            log_agent_event(
                "MCP Tool",
                "Succeeded" if resp_ok else "Failed",
                company=tool_meta.get("company"),
                conversation=conversation,
                tool_name=tool_name,
                duration_sec=elapsed(tool_started),
                http_status=resp_status,
                request=final_arguments,
                response=response_payload,
                error_message=None if resp_ok else str(response_payload)[:500],
            )
            if resp_ok:
                _record_current_reply_tool_use(tool_name)
            return resp_text

        fn = frappe.get_attr(url)
        res = fn(**arguments_dict)
        _record_current_reply_tool_use(tool_name)
        log_agent_event(
            "MCP Tool",
            "Succeeded",
            company=tool_meta.get("company"),
            conversation=conversation,
            tool_name=tool_name,
            duration_sec=elapsed(tool_started),
            request=arguments_dict,
            response=res,
        )
        return json.dumps(res)
    except Exception as e:
        log_agent_event(
            "MCP Tool",
            "Failed",
            company=tool_meta.get("company"),
            conversation=conversation,
            tool_name=tool_name,
            duration_sec=elapsed(tool_started),
            request=arguments_dict,
            error_message=str(e)[:500],
            traceback=frappe.get_traceback(),
        )
        return f"Error executing {tool_name}: {str(e)}"


def _safe_json_or_text(response):
    try:
        return response.json()
    except Exception:
        return response.text


def _request_mcp_http_with_filter_repair(url: str, method: str, arguments: dict, headers: dict):
    final_arguments = copy.deepcopy(arguments or {})
    _expand_phone_filter_variants(final_arguments)
    last_response = None
    last_payload = None

    for _attempt in range(6):
        if method == "POST":
            response = requests.post(url, json=final_arguments, headers=headers, timeout=10)
        else:
            response = requests.get(url, params=final_arguments, headers=headers, timeout=10)
        payload = _safe_json_or_text(response)
        last_response = response
        last_payload = payload

        invalid_field = _extract_field_not_permitted(payload)
        if response.ok or not invalid_field:
            return response, payload, final_arguments

        repaired = _remove_filter_field(final_arguments, invalid_field)
        if not repaired:
            return response, payload, final_arguments

    return last_response, last_payload, final_arguments


def _extract_field_not_permitted(payload) -> str:
    text = frappe.as_json(payload) if not isinstance(payload, str) else payload
    match = re.search(r"Field not permitted in query:\s*([A-Za-z0-9_]+)", text or "")
    return match.group(1) if match else ""


def _remove_filter_field(arguments: dict, fieldname: str) -> bool:
    changed = False
    for key in ("filters", "or_filters"):
        filters = arguments.get(key)
        if isinstance(filters, list):
            kept = []
            for condition in filters:
                if isinstance(condition, list) and condition and condition[0] == fieldname:
                    changed = True
                    continue
                kept.append(condition)
            arguments[key] = kept
        elif isinstance(filters, dict) and fieldname in filters:
            filters.pop(fieldname, None)
            changed = True
    return changed


def _expand_phone_filter_variants(arguments: dict) -> bool:
    phone_fields = {"mobile", "phone", "mobile_no", "phone_number", "whatsapp_number", "custom_whatsapp_number"}
    changed = False

    for key in ("filters", "or_filters"):
        filters = arguments.get(key)
        if not isinstance(filters, list):
            continue
        expanded = []
        seen = set()
        for condition in filters:
            if not (isinstance(condition, list) and len(condition) >= 3):
                marker = json.dumps(condition, default=str)
                if marker not in seen:
                    seen.add(marker)
                    expanded.append(condition)
                continue

            fieldname, operator, value = condition[0], condition[1], condition[2]
            values = [value]
            if fieldname in phone_fields and str(operator).strip() == "=":
                values = _phone_value_variants(value)
            for variant in values:
                new_condition = list(condition)
                new_condition[2] = variant
                marker = json.dumps(new_condition, default=str)
                if marker not in seen:
                    seen.add(marker)
                    expanded.append(new_condition)
                    changed = changed or variant != value
        arguments[key] = expanded

    return changed


def _phone_value_variants(value) -> list[str]:
    raw = str(value or "").strip()
    digits = re.sub(r"\D", "", raw)
    variants = []
    if raw:
        variants.append(raw)
    if len(digits) == 12 and digits.startswith("91"):
        variants.extend([digits[-10:], digits, f"+91{digits[-10:]}"])
    elif len(digits) == 10:
        variants.extend([digits, f"91{digits}", f"+91{digits}"])
    elif digits:
        variants.append(digits)
    return list(dict.fromkeys(variants))


def _resolve_mcp_http_target(tool_meta: dict) -> tuple[str, dict[str, str]]:
    endpoint = str(tool_meta.get("url") or "").strip()
    headers = {"Content-Type": "application/json"}
    server_name = tool_meta.get("server")

    if server_name and safe_ai_exists("WA MCP Server", server_name):
        server = safe_ai_get_value(
            "WA MCP Server",
            server_name,
            ["server_url"],
            as_dict=True,
        ) or {}
        auth_header = get_decrypted_password(
            "WA MCP Server",
            server_name,
            "authorization_header",
            raise_exception=False,
        )
        if auth_header:
            headers["Authorization"] = auth_header

        base_url = str(server.get("server_url") or "").strip()
        if base_url and not endpoint.startswith("http") and endpoint.startswith("/"):
            endpoint = urljoin(base_url.rstrip("/") + "/", endpoint.lstrip("/"))

    return endpoint, headers


def _max_tokens_payload_key(model_name: str | None) -> str:
    model = (model_name or "").strip().lower()
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


def _strip_model_reasoning(text: str) -> str:
    text = text or ""
    if not text:
        return ""
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    return text.strip()


def _is_low_context_provider(provider) -> bool:
    base_url = str(getattr(provider, "base_url", "") or "").lower()
    model = str(getattr(provider, "model_name", "") or "").lower()
    return "openrouter.ai" in base_url or "vllm.buopso.net" in base_url or "glm" in model


def _truncate_message_content(content, limit: int):
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        remaining = limit
        trimmed = []
        for part in content:
            if not isinstance(part, dict):
                continue
            item = dict(part)
            text = item.get("text")
            if isinstance(text, str):
                item["text"] = text[:remaining]
                remaining -= len(item["text"])
            trimmed.append(item)
            if remaining <= 0:
                break
        return trimmed
    return content


def _fit_messages_for_provider(provider, messages: list[dict]) -> list[dict]:
    if not _is_low_context_provider(provider):
        return messages

    if not messages:
        return messages

    system_message = dict(messages[0])
    system_content = str(system_message.get("content") or "")
    if len(system_content) > LOW_CONTEXT_SYSTEM_CHAR_BUDGET:
        head_budget = int(LOW_CONTEXT_SYSTEM_CHAR_BUDGET * 0.65)
        tail_budget = LOW_CONTEXT_SYSTEM_CHAR_BUDGET - head_budget
        system_message["content"] = (
            system_content[:head_budget]
            + "\n\n[Prompt middle shortened for provider context limit. Continue following ShipKia role, shipping-sales workflow, rate rules, callback rules, and stop rules. Important recent context continues below.]\n\n"
            + system_content[-tail_budget:]
        )

    kept = [system_message]
    remaining_budget = LOW_CONTEXT_INPUT_CHAR_BUDGET - len(str(system_message.get("content") or ""))
    for message in reversed(messages[1:]):
        content = message.get("content")
        content_len = len(str(content or ""))
        if remaining_budget <= 0:
            break
        if content_len > min(1200, remaining_budget):
            message = dict(message)
            message["content"] = _truncate_message_content(content, min(1200, remaining_budget))
            content_len = len(str(message.get("content") or ""))
        kept.insert(1, message)
        remaining_budget -= content_len
    return kept


def call_openai_format(provider, messages, timeout=20):
    request_started = time.monotonic()
    url = provider.base_url or "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }

    if url.endswith("/") and "chat/completions" not in url:
        url += "chat/completions"
    messages = _fit_messages_for_provider(provider, messages)

    tools = fetch_mcp_tools()
    api_tools = [{"type": t["type"], "function": t["function"]} for t in tools] if tools else None
    token_limit_key = _max_tokens_payload_key(provider.model_name)
    is_vllm_provider = "vllm.buopso.net" in str(url).lower()
    request_timeout = timeout

    output_token_limit = 900 if is_vllm_provider else 500
    payload = {
        "model": provider.model_name,
        "messages": messages,
        token_limit_key: output_token_limit,
    }
    if is_vllm_provider:
        payload["temperature"] = 0.2
    else:
        payload.update(
            {
                "temperature": 0.75,
                "presence_penalty": 0.4,
                "frequency_penalty": 0.3,
            }
        )
    if api_tools:
        payload["tools"] = api_tools

    _log_ai_timing(
        "api_request_start",
        provider=provider.name,
        provider_type=provider.provider_type,
        model=provider.model_name,
        message_count=len(messages),
        tools=1 if api_tools else 0,
        token_limit_key=token_limit_key,
        timeout_sec=request_timeout,
    )
    resp = requests.post(url, headers=headers, json=payload, timeout=request_timeout)
    _log_ai_timing(
        "api_request_done",
        provider=provider.name,
        model=provider.model_name,
        status_code=resp.status_code,
        duration_sec=elapsed(request_started),
    )

    if resp.status_code != 200:
        _safe_log_error(
            "WA AI Provider API Failure",
            f"API Error {resp.status_code}: {resp.text}",
        )
        resp.raise_for_status()

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        _safe_log_error(
            "WA AI Provider API Failure",
            f"OpenAI empty choices for model {provider.model_name}: {resp.text[:500]}",
        )
        return ""

    message = choices[0].get("message") or {}

    for tool_round in range(3):
        if not message.get("tool_calls"):
            break
        messages.append(message)

        for tc in message["tool_calls"]:
            tool_started = time.monotonic()
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}
            tool_res = execute_mcp_tool(tc["function"]["name"], args)
            _log_ai_timing(
                "tool_done",
                provider=provider.name,
                model=provider.model_name,
                tool=tc["function"]["name"],
                round=tool_round + 1,
                duration_sec=elapsed(tool_started),
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["function"]["name"],
                    "content": str(tool_res),
                }
            )

        payload["messages"] = messages
        followup_started = time.monotonic()
        _log_ai_timing(
            "api_followup_start",
            provider=provider.name,
            model=provider.model_name,
            round=tool_round + 1,
            message_count=len(messages),
        )
        resp = requests.post(url, headers=headers, json=payload, timeout=request_timeout)
        _log_ai_timing(
            "api_followup_done",
            provider=provider.name,
            model=provider.model_name,
            round=tool_round + 1,
            status_code=resp.status_code,
            duration_sec=elapsed(followup_started),
        )
        resp.raise_for_status()
        data = resp.json()
        follow_choices = data.get("choices") or []
        if not follow_choices:
            return ""
        message = follow_choices[0].get("message") or {}

    if message.get("tool_calls"):
        return "Mujhe details check karne mein thoda issue aa raha hai. Main team ko iske liye mark kar deta hoon."

    return _strip_model_reasoning(message.get("content", "") or "")
