from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import quote

import frappe
import requests

from wa_chat_hub.security import (
    WAChatHubSecurityError,
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_value,
)
from wa_chat_hub.services import normalize_phone
from wa_chat_hub.task_logger import task_log


SHIPKIA_TRACKING_URL = "https://shipkia.com/tracking_result.html?tracking_id={tracking_id}"

DELIVERY_TRACKING_RE = re.compile(
    r"\b("
    r"tracking|track|awb|waybill|shipment|parcel|courier|rto|dispatch(?:ed)?|"
    r"order\s+(?:status|kaha|kidhar|tracking|track|id|number|no)|"
    r"(?:delivery|dilivery)\s+status|"
    r"(?:mera|meri|my|apna|apni)\s+(?:order|parcel|shipment|medicine|dawai)|"
    r"(?:order|parcel|shipment|medicine|dawai).{0,30}\b(?:kaha|kidhar|where|kab|status)|"
    r"(?:kaha|kidhar|where|kab).{0,30}\b(?:order|parcel|shipment|medicine|dawai)|"
    r"deliver(?:ed)?\s+(?:hua|hui|ho|hogya|ho gaya|kab)"
    r")\b",
    re.IGNORECASE,
)

DELIVERY_SERVICE_QUESTION_RE = re.compile(
    r"\b("
    r"(?:aap|ap|kya|do you|can you|are you).{0,40}\b(?:deliver|delivery|dilivery)|"
    r"(?:deliver|delivery|dilivery).{0,40}\b(?:karte|krte|karti|available|service|provide)|"
    r"(?:home|online)\s+(?:deliver|delivery|dilivery)|"
    r"(?:medicine|dawai).{0,30}\b(?:online|home).{0,30}\b(?:deliver|delivery|dilivery)"
    r")\b",
    re.IGNORECASE,
)

BOT_CHECK_TOKENS = (
    "verifying that you are not a robot",
    "captcha",
    "cloudflare",
    "cf-browser-verification",
    "bot verification",
)

DELIVERY_VERIFY_REPLY = (
    "Delivery status verify karne ke liye SRIAAS team aapka record check karegi. "
    "Kripya registered mobile number, patient ID, order ID ya AWB number share kar dijiye."
)


@dataclass
class DeliveryStatusResult:
    found: bool
    reply: str
    patient: str | None = None
    encounter: str | None = None
    shipment: str | None = None
    awb: str | None = None
    tracking_url: str | None = None
    parsed: bool = False


def is_delivery_status_query(text: str | None) -> bool:
    """Return true for medicine/order delivery status questions."""
    body = str(text or "").strip()
    if not body:
        return False
    if DELIVERY_SERVICE_QUESTION_RE.search(body) and not _has_explicit_tracking_request(body):
        return False
    return bool(DELIVERY_TRACKING_RE.search(body))


def _has_explicit_tracking_request(text: str) -> bool:
    return bool(
        re.search(
            r"\b(tracking|track|awb|waybill|order\s+status|delivery\s+status|dilivery\s+status)\b",
            text,
            re.IGNORECASE,
        )
    )


def build_delivery_status_reply(conversation: str, latest_text: str | None = None) -> DeliveryStatusResult:
    """Resolve the WhatsApp sender to a Patient Encounter and build a shipment-status reply."""
    try:
        phone = _conversation_phone(conversation)
    except WAChatHubSecurityError:
        task_log("delivery_status", "blocked_conversation_phone", conversation=conversation)
        return DeliveryStatusResult(found=False, reply=DELIVERY_VERIFY_REPLY)

    task_log("delivery_status", "matched_intent", conversation=conversation, phone=phone)

    try:
        patient = _find_patient_by_phone(phone)
    except WAChatHubSecurityError:
        task_log("delivery_status", "blocked_patient_lookup", conversation=conversation, phone=phone)
        return DeliveryStatusResult(found=False, reply=DELIVERY_VERIFY_REPLY)

    if not patient:
        task_log("delivery_status", "patient_not_found", conversation=conversation, phone=phone)
        return DeliveryStatusResult(
            found=False,
            reply=(
                "I could not find a patient record for this WhatsApp number. "
                "Please share your registered mobile number or order ID."
            ),
        )

    try:
        encounter = _latest_shipment_encounter(patient)
    except WAChatHubSecurityError:
        task_log("delivery_status", "blocked_encounter_lookup", conversation=conversation, patient=patient)
        return DeliveryStatusResult(found=False, patient=patient, reply=DELIVERY_VERIFY_REPLY)

    if not encounter:
        task_log("delivery_status", "shipment_not_found", conversation=conversation, patient=patient)
        return DeliveryStatusResult(
            found=False,
            patient=patient,
            reply=(
                "I could not find any active medicine shipment linked to your number. "
                "Please share your order ID or AWB number."
            ),
        )

    try:
        awb = _shipment_tracking_id(encounter)
    except WAChatHubSecurityError:
        task_log(
            "delivery_status",
            "blocked_shipment_lookup",
            conversation=conversation,
            patient=patient,
            encounter=encounter.name,
        )
        return DeliveryStatusResult(found=False, patient=patient, encounter=encounter.name, reply=DELIVERY_VERIFY_REPLY)

    shipment_name = encounter.get("pe_shipkia_shipment")
    if not awb:
        task_log(
            "delivery_status",
            "tracking_id_missing",
            conversation=conversation,
            patient=patient,
            encounter=encounter.name,
            shipment=shipment_name,
        )
        return DeliveryStatusResult(
            found=True,
            patient=patient,
            encounter=encounter.name,
            shipment=shipment_name,
            reply=(
                "I found your medicine shipment record, but the tracking number is missing. "
                "Our team will check and update you shortly."
            ),
        )

    tracking_url = _tracking_url(awb)
    page = _fetch_shipkia_public_page(awb)
    if page:
        parsed = _parse_shipkia_public_page(page)
        if parsed:
            task_log(
                "delivery_status",
                "shipkia_fetch_success",
                conversation=conversation,
                patient=patient,
                encounter=encounter.name,
                awb=awb,
            )
            return DeliveryStatusResult(
                found=True,
                patient=patient,
                encounter=encounter.name,
                shipment=shipment_name,
                awb=awb,
                tracking_url=tracking_url,
                parsed=True,
                reply=_format_success_reply(parsed, awb, tracking_url),
            )

    task_log(
        "delivery_status",
        "shipkia_fetch_failed",
        conversation=conversation,
        patient=patient,
        encounter=encounter.name,
        awb=awb,
    )
    return DeliveryStatusResult(
        found=True,
        patient=patient,
        encounter=encounter.name,
        shipment=shipment_name,
        awb=awb,
        tracking_url=tracking_url,
        parsed=False,
        reply=f"Tracking URL: {tracking_url}",
    )


def _conversation_phone(conversation: str) -> str:
    contact = safe_ai_get_value("Chat Conversation", conversation, "contact")
    if not contact:
        return ""
    return normalize_phone(safe_ai_get_value("Chat Contact", contact, "phone_number") or "")


def _find_patient_by_phone(phone: str) -> str | None:
    if not phone:
        return None
    if not safe_ai_exists("DocType", "Patient"):
        return None

    last10 = phone[-10:] if len(phone) >= 10 else phone
    assert_ai_doctype_permission("Patient", "read")
    meta = frappe.get_meta("Patient")
    for fieldname in ("mobile", "phone", "mobile_no", "custom_whatsapp_number"):
        if not meta.has_field(fieldname):
            continue
        exact = safe_ai_get_value("Patient", {fieldname: phone}, "name")
        if exact:
            return exact
        rows = safe_ai_get_all(
            "Patient",
            filters={fieldname: ["like", f"%{last10}%"]},
            fields=["name", fieldname],
            limit_page_length=20,
        )
        for row in rows:
            normalized = normalize_phone(row.get(fieldname))
            if normalized == phone or normalized.endswith(last10):
                return row.name
    return None


def _latest_shipment_encounter(patient: str):
    if not safe_ai_exists("DocType", "Patient Encounter"):
        return None

    assert_ai_doctype_permission("Patient Encounter", "read")
    meta = frappe.get_meta("Patient Encounter")
    or_filters = []
    fields = [
        "name",
        "patient",
        "encounter_date",
        "modified",
    ]
    for fieldname in ("pe_shipkia_awb_number", "pe_shipkia_shipment"):
        if meta.has_field(fieldname):
            fields.append(fieldname)
            or_filters.append([fieldname, "is", "set"])
    if not or_filters:
        return None

    rows = safe_ai_get_all(
        "Patient Encounter",
        filters={"patient": patient, "docstatus": ["!=", 2]},
        or_filters=or_filters,
        fields=fields,
        order_by="encounter_date desc, modified desc",
        limit_page_length=1,
    )
    return rows[0] if rows else None


def _shipment_tracking_id(encounter) -> str:
    awb = str(encounter.get("pe_shipkia_awb_number") or "").strip()
    if awb:
        return awb

    shipment = encounter.get("pe_shipkia_shipment")
    if shipment and safe_ai_exists("Shipment Tracking Shipment", shipment):
        return str(
            safe_ai_get_value("Shipment Tracking Shipment", shipment, "shipkia_awb_number") or ""
        ).strip()
    return ""


def _tracking_url(tracking_id: str) -> str:
    return SHIPKIA_TRACKING_URL.format(tracking_id=quote(str(tracking_id or "").strip()))


def _fetch_shipkia_public_page(tracking_id: str) -> str:
    url = _tracking_url(tracking_id)
    try:
        resp = requests.get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": "Mozilla/5.0 wa-chat-hub delivery-status",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            task_log("delivery_status", "shipkia_http_error", status_code=resp.status_code, url=url)
            return ""
        text = resp.text or ""
        lowered = text.lower()
        if any(token in lowered for token in BOT_CHECK_TOKENS):
            task_log("delivery_status", "shipkia_bot_check", url=url)
            return ""
        return text
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Delivery Status Shipkia Fetch Failed")
        return ""


def _parse_shipkia_public_page(page_html: str) -> dict[str, str]:
    text = _html_to_text(page_html)
    if not text:
        return {}

    parsed = {
        "order_id": _extract_labeled_value(text, ("Shipkia Order ID", "Order ID")),
        "awb": _extract_labeled_value(text, ("Shipkia AWB Number", "AWB Number", "AWB")),
        "stage": _extract_labeled_value(text, ("Shipkia Stage", "Stage")),
        "status": _extract_labeled_value(text, ("Shipkia Status", "Status")),
        "delivery_date": _extract_labeled_value(
            text, ("Delivered On", "Delivery Date", "Delivered Date")
        ),
        "eta": _extract_labeled_value(text, ("Estimated Delivery", "Estimated")),
        "courier": _extract_labeled_value(text, ("Courier Partner", "Courier")),
    }
    if not (parsed.get("status") or parsed.get("stage") or parsed.get("courier")):
        return {}
    return {key: value for key, value in parsed.items() if value}


def _html_to_text(page_html: str) -> str:
    cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "\n", str(page_html or ""))
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</(div|p|tr|li|label|h\d|span)>", "\n", cleaned)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    lines = [" ".join(line.split()) for line in cleaned.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_labeled_value(text: str, labels: tuple[str, ...]) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for idx, line in enumerate(lines):
        line_norm = _norm_label(line)
        for label in labels:
            label_norm = _norm_label(label)
            if line_norm == label_norm and idx + 1 < len(lines):
                return _clean_value(lines[idx + 1])
            if line_norm.startswith(label_norm):
                inline = line[len(label) :].strip(" :-")
                if inline:
                    return _clean_value(inline)

    compact = "\n".join(lines)
    for label in labels:
        pattern = re.compile(
            re.escape(label) + r"\s*[:\-]?\s*([^\n]+)",
            re.IGNORECASE,
        )
        match = pattern.search(compact)
        if match:
            return _clean_value(match.group(1))
    return ""


def _norm_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _clean_value(value: str) -> str:
    value = " ".join(str(value or "").split()).strip(" :-")
    if value.lower() in {"asia/kolkata", "timezone"}:
        return ""
    return value[:160]


def _format_success_reply(parsed: dict[str, str], awb: str, tracking_url: str) -> str:
    status = _clean_tracking_status(parsed.get("status") or "available")
    parts = [f"Tracking status: {status}."]
    delivery_date = _delivery_date(parsed)
    if _is_delivered_status(status) and delivery_date:
        parts.append(f"Delivery date: {delivery_date}.")
    parts.append(f"Tracking URL: {tracking_url}")
    return " ".join(parts)


def _clean_tracking_status(status: str) -> str:
    text = " ".join(str(status or "").split()).strip(" .:-")
    return re.sub(r"\s+on$", "", text, flags=re.IGNORECASE)


def _is_delivered_status(status: str) -> bool:
    return _norm_label(status) == "delivered"


def _delivery_date(parsed: dict[str, str]) -> str:
    for key in ("delivery_date", "eta"):
        value = " ".join(str(parsed.get(key) or "").split()).strip(" .:-")
        if value and not re.search(r"[^A-Za-z0-9\s:/.-]", value):
            return value
    return ""
