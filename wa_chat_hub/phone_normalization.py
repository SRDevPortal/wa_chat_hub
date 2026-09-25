from __future__ import annotations

from typing import Optional

import phonenumbers


def _parse_mobile_phone(value: Optional[str], region: Optional[str]):
    raw = str(value or "").strip()
    digits = normalize_phone(raw)
    if not digits:
        return None
    if raw.startswith("00"):
        raw = "+" + digits[2:]
    elif not raw.startswith("+"):
        raw = digits
    try:
        number = phonenumbers.parse(raw, region)
        if not phonenumbers.is_valid_number(number) and not raw.startswith("+"):
            international = phonenumbers.parse("+" + digits, None)
            if phonenumbers.is_valid_number(international):
                number = international
        if not phonenumbers.is_possible_number(number):
            return None
    except phonenumbers.NumberParseException:
        return None
    return number


def mobile_phone_candidates(value: Optional[str], region: Optional[str] = None) -> list[str]:
    """Canonical international identity and safe legacy aliases for one country."""
    number = _parse_mobile_phone(value, region)
    if number is None:
        return []
    canonical = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)[1:]
    candidates = ["+" + canonical, "00" + canonical]
    legacy = _parse_mobile_phone(canonical, region)
    # Some full international numbers are also valid national numbers elsewhere.
    # A bare legacy value must resolve to this identity, not a different country.
    if legacy and phonenumbers.format_number(legacy, phonenumbers.PhoneNumberFormat.E164)[1:] == canonical:
        candidates.append(canonical)
    # Never treat the suffix of a foreign number as a local-number identity.
    if region and number.country_code == phonenumbers.country_code_for_region(region):
        national = phonenumbers.national_significant_number(number)
        for candidate in (national, "0" + national):
            try:
                alias = phonenumbers.parse(candidate, region)
                if phonenumbers.format_number(alias, phonenumbers.PhoneNumberFormat.E164)[1:] == canonical:
                    candidates.append(candidate)
            except phonenumbers.NumberParseException:
                pass
    return list(dict.fromkeys(candidates))


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
