"""Deterministic policy evaluation; no database or model calls."""
from __future__ import annotations

import math


FIELDS = {
    "enabled", "base_score", "inbound_message_weight", "inbound_message_cap",
    "unread_weight", "unread_cap", "priority_weights", "hot_terms", "hot_term_weight",
    "warm_terms", "warm_term_weight", "temperature_bands", "disease_media_scoring",
    "legacy_auto_update", "history_limit", "keyword_direction", "stopped_score",
}
MEDIA_FIELDS = {
    "enabled", "direction", "media_types", "require_disease_related_media",
    "disease_related_media_weight", "temperature_override", "disease_terms",
}


def validate_scoring_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError("Lead Scoring Policy must be an object")
    unknown = set(policy) - FIELDS
    if unknown:
        raise ValueError("Unsupported Lead Scoring Policy settings: " + ", ".join(sorted(unknown)))
    warnings = []
    if "legacy_auto_update" in policy:
        warnings.append("legacy_auto_update is deprecated and does not contribute to scores; use the top-level scoring rules.")
    if "enabled" in policy and not isinstance(policy["enabled"], bool):
        raise ValueError("enabled must be true or false")
    for key in ("base_score", "inbound_message_weight", "inbound_message_cap", "unread_weight",
                "unread_cap", "hot_term_weight", "warm_term_weight", "stopped_score"):
        if key in policy:
            _number(policy[key], key)
    limit = policy.get("history_limit", 0)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("history_limit must be a non-negative integer (0 means all messages)")
    if policy.get("keyword_direction", "Both") not in ("Both", "Inbound", "Outbound"):
        raise ValueError("keyword_direction must be Both, Inbound or Outbound")
    for key in ("hot_terms", "warm_terms"):
        _strings(policy.get(key, []), key)
    weights = policy.get("priority_weights", {})
    if not isinstance(weights, dict):
        raise ValueError("priority_weights must be an object")
    for key, value in weights.items():
        _number(value, "priority_weights." + key)
    bands = policy.get("temperature_bands", [])
    if not isinstance(bands, list):
        raise ValueError("temperature_bands must be a list")
    for band in bands:
        if not isinstance(band, dict) or set(band) != {"minimum", "label"}:
            raise ValueError("Each temperature band requires only minimum and label")
        _number(band["minimum"], "temperature_bands.minimum")
        if band["label"] not in ("Cold", "Warm", "Hot"):
            raise ValueError("Temperature labels must be Cold, Warm or Hot")
        if band["minimum"] > 100:
            raise ValueError("Temperature minimum cannot exceed 100")
    if len({b["minimum"] for b in bands}) != len(bands):
        raise ValueError("Temperature minimum values must be unique")
    if policy.get("enabled") and not any(b["minimum"] == 0 for b in bands):
        raise ValueError("Enabled scoring requires a temperature band starting at 0")
    media = policy.get("disease_media_scoring", {})
    if not isinstance(media, dict) or set(media) - MEDIA_FIELDS:
        raise ValueError("Unsupported disease_media_scoring settings")
    for key in ("enabled", "require_disease_related_media"):
        if key in media and not isinstance(media[key], bool):
            raise ValueError("disease_media_scoring." + key + " must be true or false")
    if media.get("direction", "Inbound") not in ("Inbound", "Outbound"):
        raise ValueError("Media direction must be Inbound or Outbound")
    _strings(media.get("media_types", []), "media_types")
    if set(media.get("media_types", [])) - {"Image", "Document", "Video", "Audio", "Voice", "Sticker"}:
        raise ValueError("Unsupported media type")
    _strings(media.get("disease_terms", []), "disease_terms")
    _number(media.get("disease_related_media_weight", 0), "disease_related_media_weight")
    override = media.get("temperature_override")
    if override and override not in {b["label"] for b in bands}:
        raise ValueError("Media temperature_override must match a configured temperature band")
    if media.get("enabled"):
        if not media.get("media_types"):
            raise ValueError("Enabled media scoring requires media_types")
        if media.get("require_disease_related_media", False) and not media.get("disease_terms"):
            raise ValueError("Disease-related media scoring requires explicit disease_terms for matching captions/OCR/transcripts")
    return warnings


def _number(value, key):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(key + " must be a finite non-negative number")


def _strings(value, key):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ValueError(key + " must be a list of non-empty strings")


def temperature(score, policy):
    return next((band["label"] for band in sorted(policy.get("temperature_bands", []),
                key=lambda b: b["minimum"], reverse=True) if score >= band["minimum"]), "")


def evaluate(policy, history, *, unread_count=0, priority=None, stopped=False):
    warnings = validate_scoring_policy(policy)
    if not policy.get("enabled"):
        raise ValueError("Disabled scoring must preserve existing values")
    inbound = sum(h.get("direction") == "Inbound" and bool(str(h.get("body") or "").strip()) for h in history)
    direction = policy.get("keyword_direction", "Both")
    joined = " ".join(str(h.get("body") or "") for h in history
                      if direction == "Both" or h.get("direction") == direction).casefold()
    matched = {kind: [t for t in policy.get(kind + "_terms", []) if t.casefold() in joined]
               for kind in ("hot", "warm")}
    parts = {
        "base": policy.get("base_score", 0),
        "inbound": min(policy.get("inbound_message_cap", 0), inbound * policy.get("inbound_message_weight", 0)),
        "unread": min(policy.get("unread_cap", 0), max(0, int(unread_count or 0)) * policy.get("unread_weight", 0)),
        "priority": policy.get("priority_weights", {}).get(priority, 0),
        "hot_terms": len(matched["hot"]) * policy.get("hot_term_weight", 0),
        "warm_terms": len(matched["warm"]) * policy.get("warm_term_weight", 0),
        "media": 0,
    }
    media = policy.get("disease_media_scoring", {})
    eligible = []
    if media.get("enabled"):
        for row in history:
            content_type = "Audio" if row.get("content_type") == "Voice" else row.get("content_type")
            types = ["Audio" if t == "Voice" else t for t in media["media_types"]]
            if row.get("direction") != media.get("direction", "Inbound") or content_type not in types:
                continue
            if not (row.get("media_url") or row.get("attachment_file")):
                continue
            evidence = (str(row.get("body") or "") + " " + str(row.get("scoring_media_text") or "")).casefold()
            if media.get("require_disease_related_media") and not any(t.casefold() in evidence for t in media["disease_terms"]):
                continue
            eligible.append(str(row.get("name") or ""))
        if eligible:
            parts["media"] = media.get("disease_related_media_weight", 0)
    score = min(100, max(0, sum(parts.values())))
    override = media.get("temperature_override") if eligible else None
    if stopped:
        score = min(100, policy.get("stopped_score", 0))
        override = None
    return score, override or temperature(score, policy), {
        "contributions": parts, "matched_terms": matched, "media_message_ids": eligible[:100],
        "media_match_count": len(eligible), "media_bonus_applied_once": bool(eligible),
        "temperature_override": override, "history_count": len(history), "inbound_count": inbound,
        "history_limit": policy.get("history_limit", 0), "keyword_direction": direction,
        "conversation_stopped": bool(stopped), "stopped_score": score if stopped else None,
        "warnings": warnings,
    }
