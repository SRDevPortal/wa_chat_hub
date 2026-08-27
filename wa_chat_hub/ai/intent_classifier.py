from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import requests

from wa_chat_hub.ai.configuration import (
    IntentDefinition,
    build_classifier_system_prompt,
    default_fallback_intent,
    load_active_intents,
    match_configured_intent,
)
from wa_chat_hub.policy import provider_endpoint, provider_timeout
from wa_chat_hub.identity import _claims_current_chat_number, _phones_from_text


@dataclass(frozen=True)
class IntentDecision:
    agent: str = "configured"
    intent: str = ""
    confidence: float = 0.0
    requires_patient_data: bool = False
    should_call_mcp: bool = False
    mcp_tool: str | None = None
    missing_fields: list[str] = field(default_factory=list)
    reason: str = ""
    source: str = "fallback"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_customer_intent(
    provider: Any | None,
    text: str | None,
    *,
    party_type: str = "Unknown",
    identity_status: str = "Unverified",
    pending_action: dict[str, Any] | None = None,
    channel_account: str | None = None,
    timeout: int | None = None,
) -> IntentDecision:
    """Classify with an administrator-owned catalog; trust no model routing fields."""
    del party_type, identity_status
    body = str(text or "").strip()
    intents = load_active_intents()
    fallback = default_fallback_intent(intents)
    if not intents:
        return IntentDecision(
            reason="No active intent configuration is available",
            source="configuration_missing",
        )
    if not body:
        return _decision_from_definition(
            fallback,
            confidence=1.0,
            reason="Empty customer message",
            source="configured_fallback",
            pending_action=pending_action,
        )

    configured_match = match_configured_intent(body, intents)
    model_decision = None
    if provider and getattr(provider, "provider_type", None) in {"OpenAI", "Custom", "Gemini"}:
        try:
            model_decision = _classify_with_provider(
                provider,
                body,
                intents=intents,
                channel_account=channel_account,
                timeout=provider_timeout(channel_account, "classification", fallback=int(timeout or 0)),
            )
        except Exception:
            model_decision = None

    # Literal business fallbacks live in WA AI Intent records. They are a trusted,
    # editable precedence layer and can correct a conflicting model classification.
    if configured_match:
        return _decision_from_definition(
            configured_match,
            confidence=max(configured_match.confidence_threshold, 0.8),
            reason="Matched administrator-configured fallback policy",
            source="configured_match",
            pending_action=pending_action,
        )
    if model_decision:
        return _apply_pending_fields(model_decision, pending_action)
    return _decision_from_definition(
        fallback,
        confidence=1.0,
        reason="Classifier unavailable or no configured literal match",
        source="configured_fallback",
        pending_action=pending_action,
    )


def classify_customer_intent_fallback(
    text: str,
    *,
    party_type: str = "Unknown",
    identity_status: str = "Unverified",
) -> IntentDecision:
    del party_type, identity_status
    intents = load_active_intents()
    selected = match_configured_intent(text, intents) or default_fallback_intent(intents)
    return _decision_from_definition(
        selected,
        confidence=max(selected.confidence_threshold, 0.8) if selected else 0.0,
        reason="Resolved by administrator-configured fallback policy",
        source="configured_match" if match_configured_intent(text, intents) else "configured_fallback",
    )


def intent_requires_patient_data(intent: str | None) -> bool:
    definition = load_active_intents().get(str(intent or "").strip())
    return bool(definition and definition.requires_patient_data)


def intent_clarification_prompt(intent: str | None) -> str:
    definition = load_active_intents().get(str(intent or "").strip())
    return str(definition.clarification_prompt if definition else "").strip()


def _identity_policy_for_route(route: Any) -> dict[str, Any]:
    bundle = getattr(route, "policy_bundle", None)
    return bundle.section("identity_policy") if bundle else {}


def _patient_party_type_for_route(route: Any) -> str:
    bundle = getattr(route, "policy_bundle", None)
    if not bundle:
        return ""
    mapping = bundle.section("party_routing_policy").get("party_type_by_reference_doctype") or {}
    return str(mapping.get("Patient") or "").strip()


def needs_patient_verification_question(route: Any, body_text: str | None) -> bool:
    """Enforce privacy using the Channel Account's configured identity rules."""
    policy = _identity_policy_for_route(route)
    verified_status = str((policy.get("statuses") or {}).get("verified") or "")
    patient_party_type = _patient_party_type_for_route(route)
    return bool(
        policy and patient_party_type
        and getattr(route, "party_type", None) == patient_party_type
        and getattr(route, "identity_status", None) != verified_status
        and getattr(route, "requires_patient_data", False)
        and not _phones_from_text(body_text, policy)
        and not _claims_current_chat_number(body_text, policy)
    )


def should_attempt_patient_verification(route: Any, body_text: str | None) -> bool:
    """Treat configured supplied-number and current-number evidence as alternatives."""
    policy = _identity_policy_for_route(route)
    verified_status = str((policy.get("statuses") or {}).get("verified") or "")
    patient_party_type = _patient_party_type_for_route(route)
    return bool(
        policy and patient_party_type
        and getattr(route, "party_type", None) == patient_party_type
        and getattr(route, "identity_status", None) != verified_status
        and getattr(route, "requires_patient_data", False)
        and (
            _phones_from_text(body_text, policy)
            or _claims_current_chat_number(body_text, policy)
        )
    )



def _classify_with_provider(
    provider: Any,
    body: str,
    *,
    intents: dict[str, IntentDefinition],
    channel_account: str | None,
    timeout: int,
) -> IntentDecision | None:
    url = provider_endpoint(provider, channel_account)
    if not url or timeout <= 0:
        return None
    if url.endswith("/") and "chat/completions" not in url:
        url += "chat/completions"
    payload = {
        "model": provider.model_name,
        "messages": [
            {"role": "system", "content": build_classifier_system_prompt(intents)},
            {"role": "user", "content": json.dumps({"message": body[:2000]}, ensure_ascii=False)},
        ],
        "temperature": 0,
        _token_limit_key(provider.model_name): 180,
    }
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    choices = response.json().get("choices") or []
    if not choices:
        return None
    raw = str((choices[0].get("message") or {}).get("content") or "")
    return _parse_decision(raw, intents)


def _parse_decision(
    raw: str,
    intents: dict[str, IntentDefinition],
) -> IntentDecision | None:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except Exception:
        return None
    intent_name = str(value.get("intent") or "").strip()
    definition = intents.get(intent_name)
    if not definition:
        return None
    try:
        confidence = max(0.0, min(1.0, float(value.get("confidence") or 0.0)))
    except Exception:
        return None
    if confidence < definition.confidence_threshold:
        return None
    return _decision_from_definition(
        definition,
        confidence=confidence,
        reason=str(value.get("reason") or "")[:300],
        source="ai",
    )


def _decision_from_definition(
    definition: IntentDefinition | None,
    *,
    confidence: float,
    reason: str,
    source: str,
    pending_action: dict[str, Any] | None = None,
) -> IntentDecision:
    if not definition:
        return IntentDecision(confidence=0.0, reason=reason, source=source)
    missing_fields: list[str] = []
    if pending_action:
        try:
            from wa_chat_hub.ai.workflow_engine import workflow_missing_fields

            missing_fields = workflow_missing_fields(pending_action)
        except Exception:
            missing_fields = []
    return IntentDecision(
        intent=definition.name,
        confidence=max(0.0, min(1.0, float(confidence))),
        requires_patient_data=definition.requires_patient_data,
        should_call_mcp=False,
        mcp_tool=None,
        missing_fields=missing_fields,
        reason=reason,
        source=source,
    )


def _apply_pending_fields(
    decision: IntentDecision,
    pending_action: dict[str, Any] | None,
) -> IntentDecision:
    if not pending_action:
        return decision
    try:
        from dataclasses import replace
        from wa_chat_hub.ai.workflow_engine import workflow_missing_fields

        return replace(decision, missing_fields=workflow_missing_fields(pending_action))
    except Exception:
        return decision


def _token_limit_key(model_name: str | None) -> str:
    model = str(model_name or "").strip().lower()
    return "max_completion_tokens" if model.startswith(("gpt-5", "o1", "o3", "o4")) else "max_tokens"
