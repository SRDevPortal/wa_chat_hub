from __future__ import annotations

from typing import Optional


def normalize_phone(value: Optional[str]) -> str:
    """Return digits only without making a country-code assumption."""
    return "".join(character for character in str(value or "") if character.isdigit())


def normalize_chat_phone(value: Optional[str]) -> str:
    """Use India's country code for local numbers, preserving international numbers."""
    raw = str(value or "").strip()
    digits = normalize_phone(raw)
    if digits.startswith("00"):
        return digits[2:]
    if not raw.startswith("+"):
        if len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10:
            return "91" + digits
    return digits


def chat_phone_candidates(value: Optional[str]) -> list[str]:
    """Include historical Indian local-number contacts without matching other countries."""
    phone = normalize_chat_phone(value)
    if not phone:
        return []
    candidates = [phone]
    if len(phone) == 12 and phone.startswith("91"):
        candidates.extend([phone[2:], "0" + phone[2:], "00" + phone])
    return candidates


def last10(value: Optional[str]) -> str:
    digits = normalize_phone(value)
    return digits[-10:] if len(digits) >= 10 else digits


def canonical_phone(value: Optional[str]) -> str:
    """Return the canonical representation maintained by the installed phone hooks."""
    digits = normalize_phone(value)
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 12 and digits.startswith("91"):
        return digits
    if len(digits) == 10:
        return "91" + digits
    if len(digits) > 10:
        return "91" + digits[-10:]
    return digits


def is_valid_phone(value: Optional[str]) -> bool:
    return len(last10(value)) == 10
