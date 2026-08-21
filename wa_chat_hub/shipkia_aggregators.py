from __future__ import annotations

import re
from difflib import SequenceMatcher


_AGGREGATOR_ALIASES: dict[str, str] = {
    "aftership": "AfterShip",
    "amazon shipping": "Amazon Shipping",
    "bigship": "BigShip",
    "clickpost": "ClickPost",
    "courier mitra": "Courier Mitra",
    "couriermitra": "Courier Mitra",
    "d2scale": "D2Scale",
    "delhivery": "Delhivery",
    "easyship": "Easyship",
    "ecom express": "Ecom Express",
    "ekart": "Ekart",
    "e kart": "Ekart",
    "eshopbox": "Eshopbox",
    "fship": "Fship",
    "ithink": "iThink Logistics",
    "ithink logistics": "iThink Logistics",
    "i think": "iThink Logistics",
    "i think logistics": "iThink Logistics",
    "nimbus": "NimbusPost",
    "nimbus post": "NimbusPost",
    "nimbuspost": "NimbusPost",
    "parcel monkey": "ParcelMonkey",
    "parcelmonkey": "ParcelMonkey",
    "pickrr": "Pickrr",
    "postmen": "Postmen",
    "sello ship": "SelloShip",
    "selloship": "SelloShip",
    "sendcloud": "Sendcloud",
    "ship bob": "ShipBob",
    "shipbob": "ShipBob",
    "ship delight": "ShipDelight",
    "shipdelight": "ShipDelight",
    "ship docket": "ShipDockets",
    "ship dockets": "ShipDockets",
    "shipdocket": "ShipDockets",
    "shipdockets": "ShipDockets",
    "ship karo": "ShipKaro",
    "shipkaro": "ShipKaro",
    "ship mozo": "Shipmozo",
    "shipmozo": "Shipmozo",
    "shipmozoz": "Shipmozo",
    "ship pigo": "Shippigo",
    "shippigo": "Shippigo",
    "ship po": "Shippo",
    "shippo": "Shippo",
    "ship prime": "ShipPrime",
    "shipprime": "ShipPrime",
    "ship rocket": "Shiprocket",
    "shiprocket": "Shiprocket",
    "shiprocket prime": "Shiprocket",
    "ship station": "ShipStation",
    "shipstation": "ShipStation",
    "ship way": "Shipway",
    "shipway": "Shipway",
    "ship x post": "ShipXPost",
    "shipxpost": "ShipXPost",
    "ship yaari": "Shipyaari",
    "shipyari": "Shipyaari",
    "shipyaari": "Shipyaari",
    "shyplite": "Shyplite",
    "vamaship": "Vamaship",
    "xpressbees": "Xpressbees",
}

_NON_AGGREGATOR_TERMS = {
    "amazon",
    "flipkart",
    "instagram",
    "manual",
    "marketplace",
    "meesho",
    "offline",
    "online",
    "shopify",
    "social",
    "website",
    "whatsapp",
    "woocommerce",
}


def normalize_shipkia_aggregator(value: str, *, allow_fuzzy: bool = True) -> str:
    text = _normalise_key(value)
    if not text or text in _NON_AGGREGATOR_TERMS:
        return ""
    if text in _AGGREGATOR_ALIASES:
        return _AGGREGATOR_ALIASES[text]

    for alias, canonical in _AGGREGATOR_ALIASES.items():
        if alias in text or text in alias:
            return canonical

    if allow_fuzzy:
        best_alias = ""
        best_score = 0.0
        for alias in _AGGREGATOR_ALIASES:
            score = SequenceMatcher(None, text, alias).ratio()
            if score > best_score:
                best_alias = alias
                best_score = score
        if best_alias and best_score >= 0.88:
            return _AGGREGATOR_ALIASES[best_alias]
    return ""


def known_shipkia_aggregator_names() -> tuple[str, ...]:
    return tuple(sorted(set(_AGGREGATOR_ALIASES.values())))


def _normalise_key(value: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", " ", str(value or "").casefold())
    return re.sub(r"\s+", " ", text).strip()
