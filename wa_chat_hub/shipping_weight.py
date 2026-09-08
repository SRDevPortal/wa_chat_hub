from __future__ import annotations

import re
from dataclasses import dataclass


# Unit spelling belongs in one normalization boundary so rate quoting, lead
# scoring, and eligibility checks cannot drift apart.
WEIGHT_UNIT_ALIASES = {
    "g": "g",
    "gm": "g",
    "gms": "g",
    "gr": "g",
    "gram": "g",
    "grams": "g",
    "kg": "kg",
    "kgs": "kg",
    "kilo": "kg",
    "kilos": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
}
_UNIT_PATTERN = "|".join(
    re.escape(unit) for unit in sorted(WEIGHT_UNIT_ALIASES, key=len, reverse=True)
)
_WEIGHT_PATTERN = re.compile(
    rf"\b(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_PATTERN})\b",
    flags=re.IGNORECASE,
)
_OVER_LIMIT_PATTERN = re.compile(
    rf"\b(?:more\s+than|above|over|greater\s+than)\s*"
    rf"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>{_UNIT_PATTERN})\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedWeight:
    grams: int
    kilograms: float
    value: float
    unit: str


def extract_weights(text: str | None) -> list[ParsedWeight]:
    weights: list[ParsedWeight] = []
    for match in _WEIGHT_PATTERN.finditer(str(text or "")):
        value = float(match.group("value"))
        unit = WEIGHT_UNIT_ALIASES[match.group("unit").lower()]
        kilograms = value if unit == "kg" else value / 1000
        weights.append(
            ParsedWeight(
                grams=int(round(kilograms * 1000)),
                kilograms=kilograms,
                value=value,
                unit=unit,
            )
        )
    return weights


def parse_weight_grams(text: str | None) -> int | None:
    weights = extract_weights(text)
    return weights[0].grams if weights else None


def parse_max_weight_kg(text: str | None) -> float | None:
    weights = extract_weights(text)
    return max((weight.kilograms for weight in weights), default=None)


def parse_plain_weight_kg(text: str | None) -> float | None:
    match = re.fullmatch(
        r"\s*(?:approx(?:imately)?|around|lagbhag)?\s*(\d+(?:\.\d+)?)\s*",
        str(text or "").strip().lower(),
    )
    return float(match.group(1)) if match else None


def exceeds_package_limit(text: str | None, limit_kg: float) -> bool:
    maximum = parse_max_weight_kg(text)
    if maximum is not None and maximum > limit_kg:
        return True

    # "More than 15 kg" is over the boundary even though the literal number is
    # exactly the configured limit.
    for match in _OVER_LIMIT_PATTERN.finditer(str(text or "")):
        value = float(match.group("value"))
        unit = WEIGHT_UNIT_ALIASES[match.group("unit").lower()]
        kilograms = value if unit == "kg" else value / 1000
        if kilograms >= limit_kg:
            return True
    return False
