from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import frappe

from wa_chat_hub.security import safe_ai_get_all, safe_ai_get_doc


@dataclass(frozen=True)
class IntentDefinition:
    name: str
    label: str
    description: str
    examples: tuple[str, ...] = ()
    fallback_match: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    confidence_threshold: float = 0.55
    requires_patient_data: bool = False
    is_default_fallback: bool = False
    clarification_prompt: str = ""

    @property
    def data_scope(self) -> str:
        return "Protected" if self.requires_patient_data else "Public"


@dataclass(frozen=True)
class IntentRouteDefinition:
    name: str
    target_type: str = "Channel Account Default"
    agent_profile: str | None = None
    ai_workflow: str | None = None
    preferred_mcp_tool: str | None = None
    blocked_reply: str = ""
    blocked_replies: dict[str, str] = field(default_factory=dict)
    priority: int = 0
    specificity: int = 0


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    version: int
    expiry_minutes: int
    action_tool: str
    definition: dict[str, Any]


def load_active_intents() -> dict[str, IntentDefinition]:
    if not _doctype_available("WA AI Intent"):
        return {}
    rows = safe_ai_get_all(
        "WA AI Intent",
        filters={"is_active": 1},
        fields=[
            "name",
            "intent_name",
            "intent_label",
            "description",
            "classifier_examples",
            "fallback_match",
            "priority",
            "confidence_threshold",
            "requires_patient_data",
            "is_default_fallback",
            "clarification_prompt",
        ],
        order_by="priority desc, name asc",
        limit_page_length=500,
    )
    result: dict[str, IntentDefinition] = {}
    for row in rows:
        name = str(row.get("intent_name") or row.get("name") or "").strip()
        if not name:
            continue
        examples = tuple(
            line.strip()
            for line in str(row.get("classifier_examples") or "").splitlines()
            if line.strip()
        )
        result[name] = IntentDefinition(
            name=name,
            label=str(row.get("intent_label") or name).strip(),
            description=str(row.get("description") or "").strip(),
            examples=examples,
            fallback_match=_json_object(row.get("fallback_match")),
            priority=int(row.get("priority") or 0),
            confidence_threshold=max(0.0, min(1.0, float(row.get("confidence_threshold") or 0.0))),
            requires_patient_data=bool(row.get("requires_patient_data")),
            is_default_fallback=bool(row.get("is_default_fallback")),
            clarification_prompt=str(row.get("clarification_prompt") or "").strip(),
        )
    return result


def build_classifier_system_prompt(intents: dict[str, IntentDefinition]) -> str:
    catalog = []
    for item in sorted(intents.values(), key=lambda value: (-value.priority, value.name)):
        entry = {
            "intent": item.name,
            "meaning": item.description,
            "examples": list(item.examples[:20]),
        }
        catalog.append(entry)
    return (
        "Classify the latest customer message using the configured intent catalog below. "
        "Return exactly one JSON object and no markdown with keys intent, confidence, and reason. "
        "Use only an intent key from the catalog. Do not select an agent, tool, privacy policy, "
        "or action; trusted server configuration handles those decisions. When wording is mixed, "
        "use the most specific catalog meaning and examples.\n\n"
        f"Configured intent catalog:\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
        'Schema: {"intent":"configured_key","confidence":0.0,"reason":"short explanation"}'
    )


def default_fallback_intent(intents: dict[str, IntentDefinition]) -> IntentDefinition | None:
    configured = [item for item in intents.values() if item.is_default_fallback]
    if configured:
        return sorted(configured, key=lambda item: (-item.priority, item.name))[0]
    return None


def match_configured_intent(
    text: str | None,
    intents: dict[str, IntentDefinition],
) -> IntentDefinition | None:
    normalized = normalize_match_text(text)
    if not normalized:
        return None
    matches = [
        item
        for item in intents.values()
        if item.fallback_match and _rule_matches(normalized, item.fallback_match)
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: (-item.priority, item.name))[0]


def resolve_intent_route(
    intent: IntentDefinition,
    *,
    channel_account: str | None,
    party_type: str | None,
    identity_status: str | None,
) -> IntentRouteDefinition | None:
    if not _doctype_available("WA AI Intent Route"):
        return None
    rows = safe_ai_get_all(
        "WA AI Intent Route",
        filters={"is_active": 1},
        fields=[
            "name",
            "priority",
            "intent",
            "data_scope",
            "channel_account",
            "party_type",
            "identity_status",
            "target_type",
            "agent_profile",
            "ai_workflow",
            "preferred_mcp_tool",
            "blocked_reply",
            "blocked_replies",
        ],
        limit_page_length=500,
    )
    candidates: list[IntentRouteDefinition] = []
    actual = {
        "intent": intent.name,
        "data_scope": intent.data_scope,
        "channel_account": str(channel_account or ""),
        "party_type": str(party_type or ""),
        "identity_status": str(identity_status or ""),
    }
    for row in rows:
        scope_values = {
            "intent": str(row.get("intent") or ""),
            "data_scope": str(row.get("data_scope") or ""),
            "channel_account": str(row.get("channel_account") or ""),
            "party_type": str(row.get("party_type") or ""),
            "identity_status": str(row.get("identity_status") or ""),
        }
        if any(value and value != actual[key] for key, value in scope_values.items()):
            continue
        candidates.append(
            IntentRouteDefinition(
                name=str(row.get("name") or ""),
                target_type=str(row.get("target_type") or "Channel Account Default"),
                agent_profile=str(row.get("agent_profile") or "").strip() or None,
                ai_workflow=str(row.get("ai_workflow") or "").strip() or None,
                preferred_mcp_tool=str(row.get("preferred_mcp_tool") or "").strip() or None,
                blocked_reply=str(row.get("blocked_reply") or "").strip(),
                blocked_replies={
                    str(code).strip(): str(reply).strip()
                    for code, reply in _json_object(row.get("blocked_replies")).items()
                    if str(code).strip() and str(reply).strip()
                },
                priority=int(row.get("priority") or 0),
                specificity=sum(1 for value in scope_values.values() if value),
            )
        )
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (-item.specificity, -item.priority, item.name))[0]


def load_workflow(name: str | None) -> WorkflowDefinition | None:
    workflow_name = str(name or "").strip()
    if not workflow_name or not _doctype_available("WA AI Workflow"):
        return None
    try:
        doc = safe_ai_get_doc("WA AI Workflow", workflow_name)
    except Exception:
        return None
    if not doc.get("is_active"):
        return None
    definition = _json_object(doc.get("definition"))
    if not definition:
        return None
    return WorkflowDefinition(
        name=doc.name,
        version=max(1, int(doc.get("workflow_version") or 1)),
        expiry_minutes=max(1, int(doc.get("expiry_minutes") or 1)),
        action_tool=str(doc.get("action_tool") or "").strip(),
        definition=definition,
    )


def active_workflow_action_tools() -> set[str]:
    if not _doctype_available("WA AI Workflow"):
        return set()
    return {
        str(value).strip()
        for value in safe_ai_get_all(
            "WA AI Workflow",
            pluck="action_tool",
            limit_page_length=500,
        )
        if str(value or "").strip()
    }


def normalize_match_text(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _rule_matches(text: str, rule: dict[str, Any]) -> bool:
    exact = [_normalized_phrase(item) for item in rule.get("full_text_any") or []]
    any_phrases = [_normalized_phrase(item) for item in rule.get("contains_any") or []]
    excludes = [_normalized_phrase(item) for item in rule.get("excludes_any") or []]
    all_groups = rule.get("contains_all") or []

    if any(phrase and phrase in text for phrase in excludes):
        return False
    positive_rules = False
    matched = False
    if exact:
        positive_rules = True
        matched = matched or text in exact
    if any_phrases:
        positive_rules = True
        matched = matched or any(phrase and phrase in text for phrase in any_phrases)
    if all_groups:
        positive_rules = True
        matched = matched or any(
            isinstance(group, list)
            and group
            and all(_normalized_phrase(phrase) in text for phrase in group if _normalized_phrase(phrase))
            for group in all_groups
        )
    return positive_rules and matched


def _normalized_phrase(value: Any) -> str:
    return normalize_match_text(str(value or ""))


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = frappe.parse_json(raw)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _doctype_available(doctype: str) -> bool:
    try:
        return bool(frappe.db.exists("DocType", doctype))
    except Exception:
        return False
