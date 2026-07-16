from __future__ import annotations

from typing import Optional


def normalize_phone(value: Optional[str]) -> str:
    """Return digits only without making a country-code assumption."""
    return "".join(character for character in str(value or "") if character.isdigit())


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
