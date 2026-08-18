from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

import frappe

from wa_chat_hub.ai.language import resolve_language_from_history
from wa_chat_hub.db_retry import with_db_lock_retry
from wa_chat_hub.prompts import get_conversation_crm_lead
from wa_chat_hub.policy import get_conversation_policy
from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_set_value,
)


@dataclass
class ScoreResult:
    lead_score: float
    lead_temperature: str
    lead_lan: str
    source: str


def score_and_sync_conversation(conversation: str) -> Dict[str, str]:
    result = recompute_conversation_metrics(conversation)
    sync_to_linked_lead(conversation, result)
    sync_to_conversation(conversation, result)
    return {
        "lead_score": f"{result.lead_score:.2f}",
        "lead_temperature": result.lead_temperature,
        "lead_lan": result.lead_lan,
        "source": result.source,
    }


def recompute_conversation_metrics(conversation: str) -> ScoreResult:
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    if getattr(convo, "conversation_stopped", 0):
        return ScoreResult(
            lead_score=0,
            lead_temperature="Cold",
            lead_lan=convo.lead_lan or "English",
            source="conversation_stopped",
        )

    history = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["direction", "body", "creation"],
        order_by="creation asc",
        limit_page_length=40,
    )
    score_result = _policy_score(convo, history)
    return score_result


def sync_to_conversation(conversation: str, result: ScoreResult) -> None:
    """Persist scoring only when at least one derived value changed."""
    assert_ai_doctype_permission("Chat Conversation", "write")
    with_db_lock_retry(
        "conversation_lead_score_update",
        lambda: frappe.db.sql(
            """
            UPDATE `tabChat Conversation`
            SET lead_score = %s,
                lead_temperature = %s,
                lead_lan = %s
            WHERE name = %s
              AND (
                  NOT (lead_score <=> %s)
                  OR NOT (lead_temperature <=> %s)
                  OR NOT (lead_lan <=> %s)
              )
            """,
            (
                result.lead_score,
                result.lead_temperature,
                result.lead_lan,
                conversation,
                result.lead_score,
                result.lead_temperature,
                result.lead_lan,
            ),
        ),
    )


def sync_to_linked_lead(conversation: str, result: ScoreResult | None = None) -> None:
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    crm_lead = get_conversation_crm_lead(convo)
    if not crm_lead:
        return

    if result is None:
        result = ScoreResult(
            lead_score=float(convo.lead_score or 0),
            lead_temperature=convo.lead_temperature or "Cold",
            lead_lan=convo.lead_lan or "English",
            source="existing",
        )

    target_dt = "CRM Lead"
    if not safe_ai_exists(target_dt, crm_lead):
        return

    assert_ai_doctype_permission(target_dt, "read")
    meta = frappe.get_meta(target_dt)
    updates = {}
    if meta.has_field("lead_score"):
        updates["lead_score"] = result.lead_score
    if meta.has_field("lead_lan"):
        updates["lead_lan"] = result.lead_lan
    if meta.has_field("lead_temperature"):
        updates["lead_temperature"] = result.lead_temperature
    if not updates:
        return

    safe_ai_set_value(
        target_dt,
        crm_lead,
        updates,
        update_modified=False,
    )


def _policy_score(convo, history: List[Dict]) -> ScoreResult:
    bundle = get_conversation_policy(convo)
    scoring_policy = bundle.section("lead_scoring_policy") if bundle else {}
    if not scoring_policy.get("enabled"):
        return ScoreResult(
            lead_score=float(getattr(convo, "lead_score", 0) or 0),
            lead_temperature=str(getattr(convo, "lead_temperature", "") or ""),
            lead_lan=str(getattr(convo, "lead_lan", "") or ""),
            source="policy_disabled",
        )
    inbound = [h for h in history if h.get("direction") == "Inbound" and str(h.get("body") or "").strip()]
    latest_text = inbound[-1]["body"] if inbound else ""
    lang = resolve_language_from_history(
        str(latest_text or ""), history, channel_account=getattr(convo, "channel_account", None)
    )
    lead_lan = str(lang.get("label") or "").strip()

    return _policy_rule_score(convo, history, lead_lan, scoring_policy)


def _policy_rule_score(convo, history: List[Dict], lead_lan: str, policy: Dict) -> ScoreResult:
    score = float(policy.get("base_score") or 0)
    inbound_count = len([h for h in history if h.get("direction") == "Inbound" and str(h.get("body") or "").strip()])
    score += min(float(policy.get("inbound_message_cap") or 0), inbound_count * float(policy.get("inbound_message_weight") or 0))
    score += min(float(policy.get("unread_cap") or 0), int(convo.unread_count or 0) * float(policy.get("unread_weight") or 0))
    score += float((policy.get("priority_weights") or {}).get(convo.priority) or 0)

    joined = " ".join([str(h.get("body") or "") for h in history]).lower()
    hot_terms = [str(term).lower() for term in policy.get("hot_terms") or []]
    warm_terms = [str(term).lower() for term in policy.get("warm_terms") or []]
    score += sum(float(policy.get("hot_term_weight") or 0) for term in hot_terms if term in joined)
    score += sum(float(policy.get("warm_term_weight") or 0) for term in warm_terms if term in joined)

    score = _clamp(score, 0, 100)
    return ScoreResult(
        lead_score=score,
        lead_temperature=_score_to_temperature(score, policy),
        lead_lan=lead_lan,
        source="policy",
    )


def _score_to_temperature(score: float, policy: Dict) -> str:
    bands = sorted(
        (band for band in policy.get("temperature_bands") or [] if isinstance(band, dict)),
        key=lambda band: float(band.get("minimum") or 0),
        reverse=True,
    )
    for band in bands:
        if score >= float(band.get("minimum") or 0):
            return str(band.get("label") or "")
    return ""


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
