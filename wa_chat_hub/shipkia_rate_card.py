from __future__ import annotations

import csv
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import frappe


ZONES = ("Zone A", "Zone B", "Zone C", "Zone D", "Zone E", "Zone F")
DEFAULT_RATE_CARD_FILE = "shipkia_rate_card_june_2026.csv"
METRO_CITIES = {"delhi", "mumbai", "bangalore", "bengaluru", "chennai", "hyderabad", "kolkata"}
NCR_CITIES = {"delhi", "new delhi", "gurgaon", "gurugram", "noida", "ghaziabad", "faridabad"}
ZONE_F_REGIONS = {
    "ladakh",
    "leh",
    "andaman",
    "nicobar",
    "andaman and nicobar",
}
ZONE_E_REGIONS = {
    "jammu",
    "jammu and kashmir",
    "kashmir",
    "himachal",
    "himachal pradesh",
    "assam",
    "arunachal",
    "arunachal pradesh",
    "manipur",
    "meghalaya",
    "mizoram",
    "nagaland",
    "sikkim",
    "tripura",
}
CITY_ALIASES = {
    "new delhi": "delhi",
    "gurugram": "gurgaon",
    "bengaluru": "bangalore",
    "banglore": "bangalore",
    "kolkta": "kolkata",
    "calcutta": "kolkata",
}
ROUTE_CONNECTOR_PATTERN = r"\b(?:from\s+)?([A-Za-z][A-Za-z .'-]{1,40}?)\s+(?:to|se)\s+([A-Za-z][A-Za-z .'-]{1,40})"
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
    "shipro",
    "shiprocket",
    "nimbuspost",
    "provider",
}
CITY_STATE = {
    "delhi": "Delhi",
    "gurgaon": "Haryana",
    "noida": "Uttar Pradesh",
    "ghaziabad": "Uttar Pradesh",
    "faridabad": "Haryana",
    "mumbai": "Maharashtra",
    "pune": "Maharashtra",
    "nagpur": "Maharashtra",
    "thane": "Maharashtra",
    "bangalore": "Karnataka",
    "mysuru": "Karnataka",
    "mysore": "Karnataka",
    "chennai": "Tamil Nadu",
    "coimbatore": "Tamil Nadu",
    "hyderabad": "Telangana",
    "kolkata": "West Bengal",
    "howrah": "West Bengal",
    "lucknow": "Uttar Pradesh",
    "kanpur": "Uttar Pradesh",
    "agra": "Uttar Pradesh",
    "varanasi": "Uttar Pradesh",
    "jaipur": "Rajasthan",
    "udaipur": "Rajasthan",
    "jodhpur": "Rajasthan",
    "ahmedabad": "Gujarat",
    "surat": "Gujarat",
    "vadodara": "Gujarat",
    "rajkot": "Gujarat",
    "kochi": "Kerala",
    "cochin": "Kerala",
    "trivandrum": "Kerala",
    "thiruvananthapuram": "Kerala",
    "patna": "Bihar",
    "bhubaneswar": "Odisha",
    "indore": "Madhya Pradesh",
    "bhopal": "Madhya Pradesh",
    "chandigarh": "Chandigarh",
    "amritsar": "Punjab",
    "ludhiana": "Punjab",
    "dehradun": "Uttarakhand",
    "ranchi": "Jharkhand",
    "raipur": "Chhattisgarh",
    "guwahati": "Assam",
}


@dataclass(frozen=True)
class RateRow:
    courier_partner: str
    courier: str
    mode: str
    min_weight: int | None
    max_weight: int
    is_additional: bool
    rates: dict[str, float]
    cod_amount: float
    cod_percentage: float
    dph_divisor: float


@dataclass(frozen=True)
class RateOption:
    courier_partner: str
    courier: str
    mode: str
    zone: str
    weight_grams: int
    rate: float
    cod_amount: float
    cod_percentage: float
    source: str


def build_rate_context_for_message(message: str, history: Iterable | None = None) -> str:
    """Return compact, deterministic rate-card context for the reply model."""
    text = str(message or "")
    if not _looks_like_rate_query(text):
        return ""

    route = parse_route_cities(text)
    estimated_zone = estimate_zone_for_route(*route) if route else None
    zone = parse_zone(text) or estimated_zone
    weight_grams = parse_weight_grams(text) or 500
    wants_flat = _looks_like_flat_rate_query(text)
    wants_cod = bool(re.search(r"\bcod\b", text, flags=re.IGNORECASE))

    lines = [
        "SHIPKIA RATE CARD CONTEXT (Latest June default rate card).",
        "Use this rate-card data for rate answers. Do not invent rates.",
        "Quote amounts as starting/base rates and mention that final live rate can vary by exact pincode, courier availability, taxes, and extra charges.",
    ]

    if wants_flat:
        lines.append(_flat_zonal_summary(weight_grams))
    if zone:
        options = best_rate_options(zone, weight_grams, mode="FWD", limit=5)
        if options:
            route_note = ""
            if estimated_zone and route:
                route_note = f" Estimated route {route[0].title()} to {route[1].title()} as {zone}."
            lines.append(
                f"Best FWD options for {weight_grams}g in {zone}:{route_note} "
                + "; ".join(_format_option(option) for option in options)
            )
        if wants_cod:
            cod = cod_charge_summary(zone, weight_grams)
            if cod:
                lines.append(cod)
    elif not wants_flat:
        lines.append("If route/zone is missing, ask only one next detail.")

    return "\n".join(line for line in lines if line)


def build_rate_reply_for_message(message: str, history: Iterable | None = None) -> str:
    """Return a direct WhatsApp reply for explicit rate-card questions."""
    text = str(message or "")
    if not _looks_like_rate_query(text):
        return ""

    route = parse_route_cities(text)
    estimated_zone = estimate_zone_for_route(*route) if route else None
    zone = parse_zone(text) or estimated_zone
    weight_grams = parse_weight_grams(text) or 500
    wants_flat = _looks_like_flat_rate_query(text)
    wants_cod = bool(re.search(r"\bcod\b", text, flags=re.IGNORECASE))

    if wants_flat:
        return (
            f"ShipKia approved rate card ke basis par {weight_grams}g prepaid FWD starting rates: "
            f"{_flat_zonal_inline(weight_grams)}. Final live rate exact pincode, courier serviceability, taxes, dimensions aur chargeable weight ke hisaab se vary kar sakta hai."
        )

    if zone:
        options = best_rate_options(zone, weight_grams, mode="FWD", limit=3)
        if not options:
            return ""
        best = options[0]
        if estimated_zone and route:
            reply = (
                f"{route[0].title()} se {route[1].title()} ke liye estimated {zone} consider kar rahi hoon. "
                f"{weight_grams}g shipment par ShipKia rates Rs. {best.rate:g} se start hote hain ({best.courier}). "
                "Final live rate exact pincode, courier serviceability, taxes, dimensions aur chargeable weight ke hisaab se vary kar sakta hai."
            )
        else:
            reply = (
                f"{weight_grams}g shipment ke liye {zone} me ShipKia rates Rs. {best.rate:g} se start hote hain "
                f"({best.courier}). Final live rate exact pincode, courier serviceability, taxes, dimensions aur chargeable weight ke hisaab se vary kar sakta hai."
            )
        if wants_cod:
            reply += f" COD terms: Rs. {best.cod_amount:g} ya {best.cod_percentage:g}% as per courier terms."
        return reply

    if _looks_like_route_rate_query(text):
        return "Starting rate check karne ke liye pickup city share kar dijiye."

    return ""


def flat_zonal_rates(weight: str | int | float | None = None, mode: str = "FWD") -> dict[str, object]:
    weight_grams = parse_weight_grams(str(weight or "")) if not isinstance(weight, (int, float)) else int(weight)
    weight_grams = weight_grams or 500
    return {
        "weight_grams": weight_grams,
        "mode": mode.upper(),
        "zones": {
            zone: [_option_dict(option) for option in best_rate_options(zone, weight_grams, mode=mode, limit=3)]
            for zone in ZONES
        },
    }


def best_rate_options(zone: str, weight_grams: int, mode: str = "FWD", limit: int = 5) -> list[RateOption]:
    zone = _normalize_zone(zone)
    mode = str(mode or "FWD").strip().upper()
    weight_grams = int(weight_grams or 500)
    options = [
        option
        for option in (
            _rate_for_service(rows, zone, weight_grams, mode)
            for rows in _service_groups().values()
        )
        if option is not None
    ]
    options.sort(key=lambda option: (option.rate, option.courier))
    return options[: max(1, int(limit or 5))]


def cod_charge_summary(zone: str, weight_grams: int) -> str:
    options = best_rate_options(zone, weight_grams, mode="FWD", limit=1)
    if not options:
        return ""
    option = options[0]
    return (
        f"COD for the best option uses COD amount Rs. {option.cod_amount:g} "
        f"or {option.cod_percentage:g}% as per courier terms."
    )


def parse_zone(text: str | None) -> str | None:
    value = str(text or "")
    match = re.search(r"\bzone\s*([a-f])\b|\b([a-f])\s*zone\b", value, flags=re.IGNORECASE)
    if not match:
        return None
    return f"Zone {str(match.group(1) or match.group(2)).upper()}"


def parse_route_cities(text: str | None) -> tuple[str, str] | None:
    matches = list(
        re.finditer(
            ROUTE_CONNECTOR_PATTERN,
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )
    if not matches:
        return None
    for match in reversed(matches):
        pickup_raw = match.group(1)
        delivery_raw = match.group(2)
        if not _looks_like_real_route_match(pickup_raw, delivery_raw, str(text or "")):
            continue
        pickup = _normalize_city(pickup_raw)
        delivery = _normalize_city(delivery_raw)
        if _is_city_candidate(pickup) and _is_city_candidate(delivery):
            return pickup, delivery
    return None


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
    normalized_full = re.sub(r"\s+", " ", str(full_text or "").strip().lower())
    if re.search(r"\b(?:use|using|aggregator|provider|courier)\b", normalized_full):
        return bool(_known_city_from_phrase(pickup) and _known_city_from_phrase(delivery))
    return True


def estimate_zone_for_route(pickup_city: str, delivery_city: str) -> str:
    pickup = _normalize_city(pickup_city)
    delivery = _normalize_city(delivery_city)
    if pickup == delivery:
        return "Zone A"
    if delivery in ZONE_F_REGIONS:
        return "Zone F"
    if delivery in ZONE_E_REGIONS:
        return "Zone E"
    if pickup in NCR_CITIES and delivery in NCR_CITIES:
        return "Zone B"
    if CITY_STATE.get(pickup) and CITY_STATE.get(pickup) == CITY_STATE.get(delivery):
        return "Zone B"
    if pickup in METRO_CITIES and delivery in METRO_CITIES:
        return "Zone C"
    return "Zone D"


def parse_weight_grams(text: str | None) -> int | None:
    value = str(text or "").lower()
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(kg|kgs|kilogram|kilograms)\b", value)
    if match:
        return int(math.ceil(float(match.group(1)) * 1000))
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(g|gm|gram|grams)\b", value)
    if match:
        return int(math.ceil(float(match.group(1))))
    return None


def rate_card_stats() -> dict[str, object]:
    rows = _load_rate_rows()
    return {
        "rows": len(rows),
        "courier_partners": sorted({row.courier_partner for row in rows}),
        "couriers": sorted({row.courier for row in rows}),
        "modes": sorted({row.mode for row in rows}),
    }


def _flat_zonal_summary(weight_grams: int) -> str:
    parts = []
    for zone in ZONES:
        options = best_rate_options(zone, weight_grams, mode="FWD", limit=1)
        if not options:
            continue
        option = options[0]
        parts.append(f"{zone[-1]} Rs. {option.rate:g} ({option.courier})")
    return f"Flat zonal starting FWD rates for {weight_grams}g: " + ", ".join(parts)


def _flat_zonal_inline(weight_grams: int) -> str:
    parts = []
    for zone in ZONES:
        options = best_rate_options(zone, weight_grams, mode="FWD", limit=1)
        if options:
            parts.append(f"Zone {zone[-1]} Rs. {options[0].rate:g}")
    return ", ".join(parts)


def _rate_for_service(rows: list[RateRow], zone: str, weight_grams: int, mode: str) -> RateOption | None:
    service_rows = [row for row in rows if row.mode == mode]
    if not service_rows:
        return None

    explicit = [
        row
        for row in service_rows
        if not row.is_additional
        and row.min_weight is not None
        and row.min_weight <= weight_grams <= row.max_weight
        and row.rates.get(zone, 0) > 0
    ]
    if explicit:
        row = sorted(explicit, key=lambda item: (item.rates[zone], item.max_weight))[0]
        return _option_from_row(row, zone, weight_grams, row.rates[zone], "slab")

    base_rows = [
        row
        for row in service_rows
        if not row.is_additional
        and row.min_weight == 0
        and row.rates.get(zone, 0) > 0
    ]
    additional_rows = [
        row
        for row in service_rows
        if row.is_additional and row.max_weight > 0 and row.rates.get(zone, 0) > 0
    ]
    if not base_rows or not additional_rows:
        return None

    base = sorted(base_rows, key=lambda item: item.max_weight)[0]
    additional = sorted(additional_rows, key=lambda item: item.max_weight)[0]
    extra_weight = max(0, weight_grams - base.max_weight)
    extra_units = math.ceil(extra_weight / additional.max_weight) if extra_weight else 0
    rate = base.rates[zone] + (extra_units * additional.rates[zone])
    return _option_from_row(base, zone, weight_grams, rate, f"base+{extra_units}x{additional.max_weight}g")


def _option_from_row(row: RateRow, zone: str, weight_grams: int, rate: float, source: str) -> RateOption:
    return RateOption(
        courier_partner=row.courier_partner,
        courier=row.courier,
        mode=row.mode,
        zone=zone,
        weight_grams=weight_grams,
        rate=round(float(rate), 2),
        cod_amount=row.cod_amount,
        cod_percentage=row.cod_percentage,
        source=source,
    )


def _format_option(option: RateOption) -> str:
    return f"{option.courier} Rs. {option.rate:g}"


def _option_dict(option: RateOption) -> dict[str, object]:
    return {
        "courier_partner": option.courier_partner,
        "courier": option.courier,
        "mode": option.mode,
        "zone": option.zone,
        "weight_grams": option.weight_grams,
        "rate": option.rate,
        "cod_amount": option.cod_amount,
        "cod_percentage": option.cod_percentage,
        "source": option.source,
    }


@lru_cache(maxsize=1)
def _load_rate_rows() -> tuple[RateRow, ...]:
    rows = []
    with _rate_card_path().open(newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            rows.append(_parse_row(raw))
    return tuple(rows)


@lru_cache(maxsize=1)
def _service_groups() -> dict[tuple[str, str], list[RateRow]]:
    groups: dict[tuple[str, str], list[RateRow]] = {}
    for row in _load_rate_rows():
        groups.setdefault((row.courier_partner, row.courier), []).append(row)
    return groups


def _parse_row(raw: dict[str, str]) -> RateRow:
    min_weight_raw = str(raw.get("Min weight") or "").strip()
    is_additional = min_weight_raw == "+"
    min_weight = None if is_additional else int(float(min_weight_raw or 0))
    return RateRow(
        courier_partner=str(raw.get("Courier Partner") or "").strip(),
        courier=str(raw.get("Couriers") or "").strip(),
        mode=str(raw.get("Mode") or "").strip().upper(),
        min_weight=min_weight,
        max_weight=int(float(str(raw.get("Max weight") or 0).strip() or 0)),
        is_additional=is_additional,
        rates={zone: _float(raw.get(zone)) for zone in ZONES},
        cod_amount=_float(raw.get("COD Amount")),
        cod_percentage=_float(raw.get("COD Percentage")),
        dph_divisor=_float(raw.get("DPH Divisor ") or raw.get("DPH Divisor")),
    )


def _rate_card_path() -> Path:
    return Path(frappe.get_app_path("wa_chat_hub", "config", DEFAULT_RATE_CARD_FILE))


def _looks_like_rate_query(text: str) -> bool:
    value = str(text or "").strip().lower()
    if not value:
        return False
    if re.search(
        r"\b(flat|zonal|zone[- ]?wise|rate card|courier charge|shipping cost|pricing|price|charge|charges|kitna\s+(?:pdega|padega|padta|pdta))\b",
        value,
        flags=re.IGNORECASE,
    ):
        return True
    if parse_route_cities(value) and (parse_weight_grams(value) or re.search(r"\brate[s]?\b", value)):
        return True
    if not re.search(r"\brate[s]?\b", value):
        return False
    return bool(
        re.search(
            r"\b(?:batao|bataiye|bataye|batana|send|share|show|dikhao|kya|kitna|kitne|chaiye|chahiye|dijiye|dijea|card|list|zone\s*[a-f]|[a-f]\s*zone)\b",
            value,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_flat_rate_query(text: str) -> bool:
    return bool(re.search(r"\b(flat|zonal|zone[- ]?wise|rate card|card)\b", str(text or ""), flags=re.IGNORECASE))


def _looks_like_route_rate_query(text: str) -> bool:
    return parse_route_cities(text) is not None


def _normalize_city(city: str | None) -> str:
    value = re.sub(r"\s+", " ", str(city or "").strip().lower().strip(".,?!:;"))
    value = re.sub(r"^(?:mujhe|muje|from)\s+", "", value).strip()
    extracted = _known_city_from_phrase(value)
    if extracted:
        return extracted
    value = re.split(
        r"\s+(?:kitna|kitne|rate|rates|charge|charges|pdega|padta|shipkia|pr|par|ka|ke|k|liye|lia|mein|me|cod|prepaid)\b",
        value,
        maxsplit=1,
    )[0].strip()
    return CITY_ALIASES.get(value, value)


def _known_city_from_phrase(value: str) -> str | None:
    words = re.findall(r"[a-z]+", str(value or "").lower())
    if not words:
        return None
    candidates = set(CITY_STATE) | set(NCR_CITIES) | set(ZONE_E_REGIONS) | set(ZONE_F_REGIONS) | set(CITY_ALIASES)
    found = []
    for size in (3, 2, 1):
        for index in range(0, len(words) - size + 1):
            phrase = " ".join(words[index : index + size])
            normalized = CITY_ALIASES.get(phrase, phrase)
            if phrase in candidates or normalized in candidates:
                found.append(normalized)
    return found[-1] if found else None


def _is_city_candidate(city: str | None) -> bool:
    value = str(city or "").strip().lower()
    if not value:
        return False
    if value in CITY_STATE or value in ZONE_E_REGIONS or value in ZONE_F_REGIONS or value in NCR_CITIES:
        return True
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
        "nimbuspost",
    }
    words = set(value.split())
    if words & blocked_words:
        return False
    return bool(re.fullmatch(r"[a-z][a-z .'-]{1,30}", value))


def _join_message_text(message: str, history: Iterable | None) -> str:
    parts = [str(message or "")]
    for row in history or []:
        body = row.get("body") if isinstance(row, dict) else getattr(row, "body", "")
        if body:
            parts.append(str(body))
    return "\n".join(parts)


def _normalize_zone(zone: str) -> str:
    parsed = parse_zone(zone)
    if parsed:
        return parsed
    value = str(zone or "").strip().upper()
    if value in {"A", "B", "C", "D", "E", "F"}:
        return f"Zone {value}"
    if value.title() in ZONES:
        return value.title()
    raise ValueError(f"Unknown ShipKia rate zone: {zone}")


def _float(value: object) -> float:
    value = str(value or "").strip()
    if not value:
        return 0.0
    return float(value.replace(",", ""))
