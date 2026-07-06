from __future__ import annotations

import re
from dataclasses import dataclass

import frappe

from wa_chat_hub.security import (
    WAChatHubSecurityError,
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_value,
)
from wa_chat_hub.services import normalize_phone
from wa_chat_hub.task_logger import task_log


CLINICAL_HISTORY_RE = re.compile(
    r"\b("
    r"clinical\s+history|medical\s+history|past\s+(?:history|medication|medicine|treatment)|"
    r"case\s+history|patient\s+history|treatment\s+history|old\s+records?|previous\s+records?|"
    r"visit\s+history|consultation\s+history|encounter\s+history|doctor\s+notes?|"
    r"(?:last|latest|recent)\s+(?:details?|record|visit|consultation|appointment|prescription|treatment|medicine|medicines|dawai|dawa)|"
    r"(?:old|previous|last|purani|pichli|pehle(?:\s+wali)?)\s+"
    r"(?:medicine|medicines|medication|dawai|dawa|treatment|prescription|history|notes|record|report|visit)|"
    r"(?:medicine|medicines|medication|dawai|dawa|prescription).{0,35}"
    r"(?:history|old|previous|last|purani|pichli|pehle|di|di thi|given|prescribed|prescribe)|"
    r"(?:meri|mera|mere|my)\s+"
    r"(?:history|medicine|medicines|medication|dawai|dawa|prescription|file|record|records|notes)|"
    r"(?:kya|what|which|kaunsi|konsi).{0,45}"
    r"(?:medicine|medicines|dawai|dawa|treatment|prescription).{0,45}"
    r"(?:di|di thi|given|prescribed|prescribe|chal rahi|chalti)"
    r")\b",
    re.IGNORECASE,
)

MEDICATION_QUERY_RE = re.compile(
    r"\b(medicine|medicines|medication|meds|dawai|dawa|prescription|rx|dose|dosage|tablet|capsule|goli|syrup)\b",
    re.IGNORECASE,
)

ORDER_QUERY_RE = re.compile(
    r"\b(order|orders|items?|product|products|medicine\s+order|last\s+order|pichla\s+order|pichli\s+order)\b",
    re.IGNORECASE,
)

LAST_ONLY_RE = re.compile(
    r"\b("
    r"last|latest|recent|newest|current|"
    r"pichla|pichli|pichle|pichhle|purana|purani|pehle\s+wala|pehle\s+wali|"
    r"last\s+(?:record|details?|visit|order|prescription|medicine|appointment|consultation|follow\s*up)|"
    r"latest\s+(?:record|details?|visit|order|prescription|medicine|appointment|consultation|follow\s*up)"
    r")\b",
    re.IGNORECASE,
)

HISTORY_HINT_RE = re.compile(
    r"\b("
    r"history|record|records|file|notes|prescription|rx|treatment|consultation|consult|"
    r"visit|appointment|encounter|follow\s*up|followup|course|order|orders|"
    r"purani|purana|pichli|pichla|pehle|pehle\s+wali|last|previous|old|past|"
    r"di\s+thi|diya\s+tha|mili\s+thi|bataya\s+tha|chal\s+rahi|chalti"
    r")\b",
    re.IGNORECASE,
)

CLINICAL_DATA_RE = re.compile(
    r"\b("
    r"medicine|medicines|medication|meds|dawai|dawa|goli|tablet|capsule|syrup|dose|dosage|"
    r"order|orders|item|items|product|products|"
    r"diagnosis|problem|complaint|symptom|symptoms|investigation|test|tests|report|"
    r"doctor|dr|vaidya|notes|advice|instruction|diet|exercise|height|weight|age"
    r")\b",
    re.IGNORECASE,
)

REQUEST_HINT_RE = re.compile(
    r"\b("
    r"batao|bataye|batana|bhejo|send|share|show|dikhao|dekhna|dekh sakte|"
    r"what|which|when|kya|kaunsi|konsi|kab|kitni|details?|info|information"
    r")\b",
    re.IGNORECASE,
)

CLINICAL_HISTORY_PRIVACY_REPLY = (
    "Privacy ke liye main chat par patient encounter ya past medical history details share nahi kar sakta. "
    "Hamari team identity karke record/status confirm kar degi. "
    "Kripya registered mobile number ya patient ID share kar dijiye."
)


@dataclass
class ClinicalHistoryResult:
    found: bool
    reply: str
    patient: str | None = None
    patient_name: str | None = None
    encounters: list[str] | None = None
    medication_focused: bool = False
    order_focused: bool = False
    latest_only: bool = False


def is_clinical_history_query(text: str | None) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    if CLINICAL_HISTORY_RE.search(body):
        return True

    normalized = _normalize_intent_text(body)
    if not normalized:
        return False

    has_history_hint = bool(HISTORY_HINT_RE.search(normalized))
    has_clinical_data = bool(CLINICAL_DATA_RE.search(normalized))
    has_request_hint = bool(REQUEST_HINT_RE.search(normalized))

    if has_history_hint and has_clinical_data:
        return True
    if has_request_hint and has_history_hint and re.search(r"\b(meri|mera|mere|my|mujhe|me|patient)\b", normalized):
        return True
    return False


def _normalize_intent_text(text: str) -> str:
    normalized = str(text or "").lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    replacements = {
        "dawaai": "dawai",
        "dwai": "dawai",
        "dawayi": "dawai",
        "dava": "dawa",
        "davai": "dawai",
        "medicin": "medicine",
        "meds": "medicine",
        "priscription": "prescription",
        "perscription": "prescription",
        "presciption": "prescription",
        "pichle": "pichli",
        "pichley": "pichli",
        "pichhle": "pichli",
        "pichhla": "pichla",
        "purane": "purani",
        "purana": "purani",
        "pehali": "pehle",
        "pehli": "pehle",
        "phle": "pehle",
        "btana": "batana",
        "btao": "batao",
        "bta": "batao",
        "dikhao": "dikhao",
        "dikhana": "dikhao",
    }
    tokens = [replacements.get(token, token) for token in normalized.split()]
    return " ".join(tokens)


def _is_latest_only_query(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    if not normalized:
        return False
    if re.search(r"\b(all|complete|full|entire|sari|saari|sab|poori|puri|history)\b", normalized):
        return False
    return bool(LAST_ONLY_RE.search(normalized))


def build_clinical_history_reply(
    conversation: str,
    latest_text: str | None = None,
) -> ClinicalHistoryResult:
    """Resolve WhatsApp sender to Patient, but do not expose Patient Encounter history in chat."""
    try:
        phone = _conversation_phone(conversation)
    except WAChatHubSecurityError:
        task_log("clinical_history", "blocked_conversation_phone", conversation=conversation)
        phone = ""

    normalized_query = _normalize_intent_text(str(latest_text or ""))
    medication_focused = bool(MEDICATION_QUERY_RE.search(normalized_query))
    order_focused = bool(ORDER_QUERY_RE.search(normalized_query))
    latest_only = _is_latest_only_query(normalized_query)
    task_log(
        "clinical_history",
        "matched_intent",
        conversation=conversation,
        phone=phone,
        medication_focused=1 if medication_focused else 0,
        order_focused=1 if order_focused else 0,
        latest_only=1 if latest_only else 0,
    )

    try:
        patient = _find_patient_by_phone(phone)
    except WAChatHubSecurityError:
        task_log("clinical_history", "blocked_patient_lookup", conversation=conversation, phone=phone)
        patient = None

    if not patient:
        task_log("clinical_history", "patient_not_found", conversation=conversation, phone=phone)
        return ClinicalHistoryResult(
            found=False,
            medication_focused=medication_focused,
            order_focused=order_focused,
            latest_only=latest_only,
            reply=(
                "I could not find a patient record for this WhatsApp number. "
                "Please share your registered mobile number or patient ID."
            ),
        )

    try:
        patient_name = safe_ai_get_value("Patient", patient, "patient_name") or patient
    except WAChatHubSecurityError:
        task_log("clinical_history", "blocked_patient_name", conversation=conversation, patient=patient)
        patient_name = patient

    task_log(
        "clinical_history",
        "privacy_guard_reply",
        conversation=conversation,
        patient=patient,
        medication_focused=1 if medication_focused else 0,
        order_focused=1 if order_focused else 0,
        latest_only=1 if latest_only else 0,
    )
    return ClinicalHistoryResult(
        found=True,
        patient=patient,
        patient_name=patient_name,
        encounters=[],
        medication_focused=medication_focused,
        order_focused=order_focused,
        latest_only=latest_only,
        reply=CLINICAL_HISTORY_PRIVACY_REPLY,
    )


def build_clinical_history_context(
    conversation: str,
    latest_text: str | None = None,
    limit: int = 0,
) -> str:
    """Return privacy guard context for the generic LLM path."""
    if not is_clinical_history_query(latest_text):
        return ""

    return (
        "Patient history privacy guard: Do not share Patient Encounter records, encounter names, "
        "clinical notes, medicines, diagnosis, orders, visit history, or past medical history details "
        "in the customer-facing reply. Ask for registered mobile number or patient ID and say the "
        "team can verify identity and confirm records/status."
    )


def get_patient_clinical_history(patient: str, limit: int = 0) -> list[dict]:
    return []


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
