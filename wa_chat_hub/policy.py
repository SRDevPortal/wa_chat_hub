from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import frappe


POLICY_CACHE_TTL_SECONDS = 300
POLICY_MAX_SERIALIZED_BYTES = 80_000
POLICY_SECTION_FIELDS = (
    "identity_policy",
    "party_routing_policy",
    "language_policy",
    "reply_templates",
    "patient_creation_policy",
    "lead_scoring_policy",
    "media_policy",
    "provider_policy",
    "runtime_policy",
    "permission_policy",
)


@dataclass(frozen=True)
class PolicyBundle:
    name: str = ""
    version: int = 0
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)

    def section(self, fieldname: str) -> dict[str, Any]:
        value = self.sections.get(str(fieldname or "").strip())
        return dict(value) if isinstance(value, dict) else {}


def get_channel_policy(channel_account: str | None) -> PolicyBundle | None:
    account = str(channel_account or "").strip()
    if not account or not frappe.db.exists("DocType", "WA AI Policy Bundle"):
        return None
    account_meta = frappe.get_meta("Chat Channel Account")
    if not account_meta.has_field("ai_policy_bundle"):
        return None
    policy_name = frappe.get_cached_value(
        "Chat Channel Account",
        account,
        "ai_policy_bundle",
    )
    return get_policy_bundle(str(policy_name or "").strip())


def get_policy_bundle(policy_name: str | None) -> PolicyBundle | None:
    name = str(policy_name or "").strip()
    if not name:
        return None
    cache_key = _cache_key(name)
    cached = frappe.cache().get_value(cache_key)
    if isinstance(cached, str):
        try:
            cached = json.loads(cached)
        except Exception:
            cached = None
    if isinstance(cached, dict):
        return _bundle_from_mapping(cached)

    fields = ["name", "policy_version", "is_active", *POLICY_SECTION_FIELDS]
    row = frappe.db.get_value("WA AI Policy Bundle", name, fields, as_dict=True)
    if not row or not row.get("is_active"):
        return None
    mapping = {
        "name": row.get("name"),
        "version": int(row.get("policy_version") or 1),
        "sections": {
            fieldname: parse_policy_object(row.get(fieldname), fieldname=fieldname)
            for fieldname in POLICY_SECTION_FIELDS
        },
    }
    encoded = json.dumps(mapping, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= POLICY_MAX_SERIALIZED_BYTES:
        frappe.cache().set_value(
            cache_key,
            encoded,
            expires_in_sec=POLICY_CACHE_TTL_SECONDS,
        )
    return _bundle_from_mapping(mapping)


def get_conversation_policy(conversation: str | Any) -> PolicyBundle | None:
    if isinstance(conversation, str):
        channel_account = frappe.db.get_value(
            "Chat Conversation",
            conversation,
            "channel_account",
        )
    else:
        channel_account = getattr(conversation, "channel_account", None)
    return get_channel_policy(channel_account)


def policy_section(
    channel_account: str | None,
    fieldname: str,
) -> dict[str, Any]:
    bundle = get_channel_policy(channel_account)
    return bundle.section(fieldname) if bundle else {}


def policy_reply(
    channel_account: str | None,
    key: str,
    *,
    language_code: str | None = None,
) -> str:
    templates = policy_section(channel_account, "reply_templates")
    value = templates.get(str(key or "").strip())
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    code = str(language_code or "").strip()
    base_code = code.split("-", 1)[0] if code else ""
    for candidate in (code, base_code, "default"):
        reply = value.get(candidate)
        if isinstance(reply, str) and reply.strip():
            return reply.strip()
    return ""


def parse_policy_object(value: Any, *, fieldname: str = "policy") -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value))
    except Exception as exc:
        raise frappe.ValidationError(
            f"{fieldname} must contain a valid JSON object."
        ) from exc
    if not isinstance(parsed, dict):
        raise frappe.ValidationError(f"{fieldname} must contain a JSON object.")
    return parsed


def invalidate_policy_cache(policy_name: str | None) -> None:
    name = str(policy_name or "").strip()
    if name:
        frappe.cache().delete_value(_cache_key(name))


def _bundle_from_mapping(value: dict[str, Any]) -> PolicyBundle:
    sections = value.get("sections") if isinstance(value.get("sections"), dict) else {}
    return PolicyBundle(
        name=str(value.get("name") or ""),
        version=int(value.get("version") or 0),
        sections={
            fieldname: dict(section)
            for fieldname, section in sections.items()
            if isinstance(section, dict)
        },
    )


def _cache_key(policy_name: str) -> str:
    return f"wa_chat_hub:policy_bundle:v1:{policy_name}"


def provider_endpoint(provider: Any, channel_account: str | None) -> str:
    """Resolve a provider URL from its record, then the assigned policy only."""
    configured = str(
        (provider.get("base_url") if isinstance(provider, dict) else getattr(provider, "base_url", None)) or ""
    ).strip()
    if configured:
        return configured
    provider_policy = policy_section(channel_account, "provider_policy")
    endpoints = provider_policy.get("endpoints") or {}
    provider_type = (
        provider.get("provider_type") if isinstance(provider, dict) else getattr(provider, "provider_type", None)
    )
    return str(endpoints.get(str(provider_type or "")) or "").strip()


def provider_timeout(
    channel_account: str | None,
    capability: str,
    *,
    fallback: int = 0,
) -> int:
    provider_policy = policy_section(channel_account, "provider_policy")
    timeouts = provider_policy.get("request_timeouts") or {}
    try:
        value = int(timeouts.get(str(capability or "").strip()) or fallback)
    except (TypeError, ValueError):
        value = fallback
    return max(0, value)
