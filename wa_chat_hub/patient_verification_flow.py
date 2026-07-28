from __future__ import annotations

import re
from dataclasses import dataclass

import frappe
from frappe.utils import cint, now_datetime


VERIFY_REQUEST_REPLY = (
    "To access your medical information securely, please reply with your "
    "registered 10-digit mobile number."
)
VERIFY_MISMATCH_REPLY = (
    "I could not verify that number. Please check it and reply with the "
    "registered 10-digit mobile number."
)
AMBIGUOUS_IDENTITY_REPLY = (
    "I cannot safely identify the correct patient record for this WhatsApp number. "
    "I am forwarding this to our support team for secure verification."
)

PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{8,}\d)(?!\d)")
SENSITIVE_PATIENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bmedical\s+history\b",
        r"\bclinical\s+history\b",
        r"\btreatment\s+history\b",
        r"\bpatient\s+(?:history|record|details?)\b",
        r"\bmy\s+(?:history|records?|reports?|prescriptions?|appointments?)\b",
        r"\b(?:show|send|share|tell|give|check|access)\b.{0,40}"
        r"\b(?:history|records?|reports?|prescriptions?|appointments?|invoices?|orders?)\b",
        r"\b(?:meri|mera|mere|apni|apna)\b.{0,40}"
        r"\b(?:history|record|report|prescription|appointment|invoice|order|ilaj|treatment)\b",
        r"\b(?:history|record|report|prescription|appointment|invoice|order)\b.{0,40}"
        r"\b(?:batao|btao|dikhao|dikhaiye|share|send)\b",
        r"\b(?:my|meri|mera|mere)\b.{0,30}"
        r"\b(?:delivery|shipment|tracking|awb|order\s+status)\b",
        r"\b(?:delivery|shipment|tracking|awb|order\s+status)\b.{0,30}"
        r"\b(?:check|show|tell|batao|btao|kahan|where)\b",
    )
)


@dataclass(frozen=True)
class VerificationGate:
    handled: bool = False
    response: str = ""
    pending_request: str = ""
    reason: str = ""


def is_sensitive_patient_request(text: str | None) -> bool:
    value = " ".join(str(text or "").split())
    return bool(value and any(pattern.search(value) for pattern in SENSITIVE_PATIENT_PATTERNS))


def contains_phone_candidate(text: str | None) -> bool:
    return bool(PHONE_PATTERN.search(str(text or "")))


def evaluate_patient_verification_gate(
    conversation: str,
    message: str,
    body: str | None,
) -> VerificationGate:
    """Resolve the deterministic privacy gate before chat history reaches an LLM."""
    convo = frappe.get_doc("Chat Conversation", conversation)
    if getattr(convo, "party_type", None) != "Patient":
        return VerificationGate(reason="not_patient")

    identity_status = getattr(convo, "identity_status", None) or "Unverified"
    pending_request = str(getattr(convo, "pending_patient_request", None) or "").strip()

    if identity_status == "Verified":
        return VerificationGate(
            pending_request=pending_request,
            reason="verified_pending_request" if pending_request else "verified",
        )

    if identity_status == "Ambiguous":
        if is_sensitive_patient_request(body) or contains_phone_candidate(body):
            return VerificationGate(
                handled=True,
                response=AMBIGUOUS_IDENTITY_REPLY,
                reason="ambiguous_identity",
            )
        return VerificationGate(reason="ambiguous_general_request")

    if contains_phone_candidate(body):
        _increment_verification_attempts(conversation, convo)
        frappe.db.commit()
        return VerificationGate(
            handled=True,
            response=VERIFY_MISMATCH_REPLY,
            reason="registered_phone_mismatch",
        )

    if is_sensitive_patient_request(body):
        _store_pending_request(conversation, convo, message, body)
        frappe.db.commit()
        return VerificationGate(
            handled=True,
            response=VERIFY_REQUEST_REPLY,
            pending_request=str(body or "").strip(),
            reason="verification_required",
        )

    return VerificationGate(reason="patient_general_request")


def clear_pending_patient_request(conversation: str) -> None:
    meta = frappe.get_meta("Chat Conversation")
    values = {
        "pending_patient_request": None,
        "pending_request_message": None,
    }
    values = {key: value for key, value in values.items() if meta.has_field(key)}
    if values:
        frappe.db.set_value("Chat Conversation", conversation, values, update_modified=False)
        frappe.db.commit()


def _store_pending_request(conversation: str, convo, message: str, body: str | None) -> None:
    meta = frappe.get_meta("Chat Conversation")
    values = {
        "pending_patient_request": str(body or "").strip()[:4000],
        "pending_request_message": message,
        "verification_started_at": getattr(convo, "verification_started_at", None) or now_datetime(),
    }
    values = {key: value for key, value in values.items() if meta.has_field(key)}
    if values:
        frappe.db.set_value("Chat Conversation", conversation, values, update_modified=False)


def _increment_verification_attempts(conversation: str, convo) -> None:
    meta = frappe.get_meta("Chat Conversation")
    values = {
        "verification_attempts": cint(getattr(convo, "verification_attempts", 0)) + 1,
        "verification_started_at": getattr(convo, "verification_started_at", None) or now_datetime(),
    }
    values = {key: value for key, value in values.items() if meta.has_field(key)}
    if values:
        frappe.db.set_value("Chat Conversation", conversation, values, update_modified=False)
