"""Lightweight customer language detection for multilingual WhatsApp auto-replies."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Unicode script ranges (Indian languages + Arabic for Urdu)
_SCRIPT_RANGES = (
    ("hi", "Hindi", r"[\u0900-\u097F]"),
    ("bn", "Bengali", r"[\u0980-\u09FF]"),
    ("pa", "Punjabi", r"[\u0A00-\u0A7F]"),
    ("gu", "Gujarati", r"[\u0A80-\u0AFF]"),
    ("or", "Odia", r"[\u0B00-\u0B7F]"),
    ("ta", "Tamil", r"[\u0B80-\u0BFF]"),
    ("te", "Telugu", r"[\u0C00-\u0C7F]"),
    ("kn", "Kannada", r"[\u0C80-\u0CFF]"),
    ("ml", "Malayalam", r"[\u0D00-\u0D7F]"),
    ("ur", "Urdu", r"[\u0600-\u06FF]"),
)

_HINGLISH_MARKERS = frozenset(
    {
        "aap",
        "aapka",
        "aapki",
        "hai",
        "hain",
        "ho",
        "kya",
        "kyun",
        "kaise",
        "kab",
        "kahan",
        "mujhe",
        "mere",
        "mera",
        "mein",
        "main",
        "nahi",
        "nahin",
        "haan",
        "ji",
        "dhanyavad",
        "shukriya",
        "namaste",
        "doctor",
        "dawai",
        "bukhar",
        "dard",
        "appointment",
        "ilaj",
        "bataiye",
        "bataye",
        "chahiye",
        "sakta",
        "sakte",
        "kripya",
        "please",
    }
)

DEFAULT_MULTILINGUAL_POLICY = """# Multilingual replies
- Match the language and script of the customer's latest message (Hindi, Hinglish, English, Tamil, etc.).
- Sound like a native speaker chatting on WhatsApp — natural, not translated or formal.
- Do not add an English disclaimer or corporate intro unless the customer used English and asked for it."""


def _letter_count(text: str) -> int:
    letters = re.findall(r"\S", text)
    return len(letters) or 1


def _script_ratio(text: str, pattern: str) -> float:
    return len(re.findall(pattern, text)) / _letter_count(text)


def _hinglish_score(text: str) -> float:
    tokens = re.findall(r"[a-zA-Z']+", text.lower())
    if not tokens:
        return 0.0
    hits = sum(1 for token in tokens if token in _HINGLISH_MARKERS)
    return hits / len(tokens)


def detect_customer_language(text: str) -> Dict[str, Any]:
    """Infer the customer's language from their latest message text."""
    text = (text or "").strip()
    if not text:
        return {"code": "en", "label": "English", "reply_instruction": "Reply in English."}

    best_code, best_label, best_ratio = "en", "English", 0.0
    for code, label, pattern in _SCRIPT_RANGES:
        ratio = _script_ratio(text, pattern)
        if ratio > best_ratio:
            best_ratio = ratio
            best_code, best_label = code, label

    if best_ratio >= 0.15:
        return {
            "code": best_code,
            "label": best_label,
            "reply_instruction": f"Reply in {best_label} using the same script as the customer.",
        }

    hinglish = _hinglish_score(text)
    ascii_ratio = len(re.findall(r"[a-zA-Z]", text)) / _letter_count(text)
    if hinglish >= 0.12 and ascii_ratio >= 0.5:
        return {
            "code": "hi-latn",
            "label": "Hindi (Hinglish / Roman)",
            "reply_instruction": "Reply in Hinglish (Hindi written in English letters), matching the customer's tone.",
        }

    if ascii_ratio >= 0.6:
        return {
            "code": "en",
            "label": "English",
            "reply_instruction": "Reply in English.",
        }

    return {
        "code": "auto",
        "label": "customer's language",
        "reply_instruction": "Detect the customer's language from their message and reply in that same language.",
    }


def resolve_language_from_history(
    latest_text: str,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Prefer the latest inbound text; fall back to recent customer messages."""
    if str(latest_text or "").strip():
        return detect_customer_language(latest_text)

    if history:
        for row in reversed(history):
            if row.get("direction") == "Inbound" and str(row.get("body") or "").strip():
                return detect_customer_language(str(row["body"]))

    return detect_customer_language("")


def append_multilingual_instructions(
    system_prompt: str,
    customer_text: str,
    *,
    custom_policy: str | None = None,
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    policy = (custom_policy or "").strip() or DEFAULT_MULTILINGUAL_POLICY
    lang = resolve_language_from_history(customer_text, history)
    hint = lang.get("reply_instruction") or "Reply in the customer's language."

    return (
        f"{system_prompt}\n\n{policy}\n\n"
        f"# Language for this reply\n"
        f"Detected customer language: {lang.get('label', 'unknown')} ({lang.get('code', 'auto')}).\n"
        f"{hint}"
    )
