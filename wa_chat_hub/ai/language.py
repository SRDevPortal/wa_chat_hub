"""Policy-driven customer language detection for WhatsApp auto-replies."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from wa_chat_hub.policy import policy_section


def _language_policy(channel_account: str | None) -> dict[str, Any]:
    return policy_section(channel_account, "language_policy")


def _letter_count(text: str) -> int:
    return sum(1 for char in text if not char.isspace()) or 1


def _script_ratio(text: str, start: int, end: int) -> float:
    return sum(1 for char in text if start <= ord(char) <= end) / _letter_count(text)


def _roman_score(text: str, markers: set[str]) -> float:
    tokens = re.findall(r"[a-zA-Z']+", text.lower())
    if not tokens or not markers:
        return 0.0
    return sum(1 for token in tokens if token in markers) / len(tokens)


def _safe_ratio(value: Any, fallback: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return fallback


def detect_customer_language(
    text: str,
    *,
    channel_account: str | None = None,
    policy: dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Infer language only from the Channel Account's configured policy."""
    text = (text or "").strip()
    config = dict(policy) if isinstance(policy, dict) else _language_policy(channel_account)
    if not config:
        return {"code": "auto", "label": "customer's language", "reply_instruction": ""}

    default_code = str(config.get("default_language") or "auto").strip() or "auto"
    default_label = str(config.get("default_language_label") or default_code).strip()
    if not text:
        return {
            "code": default_code,
            "label": default_label,
            "reply_instruction": str(config.get("default_reply_instruction") or "").strip(),
        }

    best_code, best_label, best_ratio = default_code, default_label, 0.0
    for item in config.get("script_ranges") or []:
        if not isinstance(item, dict):
            continue
        try:
            start = int(str(item.get("start") or ""), 16)
            end = int(str(item.get("end") or ""), 16)
        except ValueError:
            continue
        ratio = _script_ratio(text, start, end)
        if ratio > best_ratio:
            best_ratio = ratio
            best_code = str(item.get("code") or default_code)
            best_label = str(item.get("label") or best_code)

    if best_ratio >= _safe_ratio(config.get("script_ratio_threshold"), 1.0):
        template = str(config.get("script_reply_instruction") or "").strip()
        return {
            "code": best_code,
            "label": best_label,
            "reply_instruction": template.format(language=best_label) if template else "",
        }

    markers = {
        str(value).strip().lower()
        for value in config.get("roman_markers") or []
        if str(value).strip()
    }
    roman_score = _roman_score(text, markers)
    ascii_ratio = sum(1 for char in text if char.isascii() and char.isalpha()) / _letter_count(text)
    if (
        roman_score >= _safe_ratio(config.get("roman_marker_ratio_threshold"), 1.0)
        and ascii_ratio >= _safe_ratio(config.get("roman_ascii_ratio_threshold"), 0.0)
    ):
        return {
            "code": str(config.get("roman_language_code") or "auto"),
            "label": str(config.get("roman_language_label") or "customer's language"),
            "reply_instruction": str(config.get("roman_reply_instruction") or "").strip(),
        }

    if ascii_ratio >= _safe_ratio(config.get("ascii_ratio_threshold"), 1.0):
        return {
            "code": str(config.get("ascii_language_code") or default_code),
            "label": str(config.get("ascii_language_label") or default_label),
            "reply_instruction": str(config.get("ascii_reply_instruction") or "").strip(),
        }

    return {
        "code": "auto",
        "label": "customer's language",
        "reply_instruction": str(config.get("auto_reply_instruction") or "").strip(),
    }


def resolve_language_from_history(
    latest_text: str,
    history: Optional[List[Dict[str, Any]]] = None,
    *,
    channel_account: str | None = None,
    policy: dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Prefer latest inbound text, then bounded history supplied by the caller."""
    if str(latest_text or "").strip():
        return detect_customer_language(latest_text, channel_account=channel_account, policy=policy)
    for row in reversed(history or []):
        if row.get("direction") == "Inbound" and str(row.get("body") or "").strip():
            return detect_customer_language(
                str(row["body"]), channel_account=channel_account, policy=policy
            )
    return detect_customer_language("", channel_account=channel_account, policy=policy)


def append_multilingual_instructions(
    system_prompt: str,
    customer_text: str,
    *,
    custom_policy: str | None = None,
    history: Optional[List[Dict[str, Any]]] = None,
    channel_account: str | None = None,
) -> str:
    config = _language_policy(channel_account)
    policy_text = (custom_policy or "").strip() or str(config.get("prompt_policy") or "").strip()
    language = resolve_language_from_history(
        customer_text, history, channel_account=channel_account, policy=config
    )
    blocks = [system_prompt]
    if policy_text:
        blocks.append(policy_text)
    hint = str(language.get("reply_instruction") or "").strip()
    if hint:
        blocks.append(hint)
    return "\n\n".join(block for block in blocks if str(block or "").strip())
