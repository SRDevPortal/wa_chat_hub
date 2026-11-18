from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Dict, List

import frappe

from wa_chat_hub.ai.language import resolve_language_from_history
from wa_chat_hub.ai.scoring_policy import evaluate, temperature, validate_scoring_policy
from wa_chat_hub.prompts import get_conversation_crm_lead
from wa_chat_hub.policy import get_conversation_policy
from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
)


@dataclass
class ScoreResult:
    lead_score: float
    lead_temperature: str
    lead_lan: str
    source: str
    details: dict = field(default_factory=dict)


def score_and_sync_conversation_job(conversation: str) -> Dict[str, str]:
    """Retry the complete scoring transaction, never just a failed statement."""
    old_timeout = frappe.db.sql("SELECT @@SESSION.innodb_lock_wait_timeout")[0][0]
    try:
        frappe.db.sql("SET SESSION innodb_lock_wait_timeout=5")
        return score_and_sync_conversation(conversation)
    except (frappe.QueryDeadlockError, frappe.QueryTimeoutError) as exc:
        # Frappe's job runner rolls back and retries with bounded backoff.
        raise frappe.RetryBackgroundJobError() from exc
    finally:
        frappe.db.sql("SET SESSION innodb_lock_wait_timeout=%s", (old_timeout,))


def score_and_sync_conversation(conversation: str) -> Dict[str, str]:
    # A locking read serializes scoring/link changes for this conversation.
    assert_ai_doctype_permission("Chat Conversation", "write")
    frappe.db.sql("SELECT name FROM `tabChat Conversation` WHERE name=%s FOR UPDATE", (conversation,))
    result = recompute_conversation_metrics(conversation)
    if result.source == "policy_disabled":
        return {"source": result.source}
    sync_to_linked_lead(conversation, result)
    sync_to_conversation(conversation, result)
    assert_ai_doctype_permission("Chat Conversation", "write")
    frappe.db.set_value("Chat Conversation", conversation, "lead_score_details",
                        json.dumps({**result.details, "scored_at": str(frappe.utils.now_datetime()),
                                    "score": result.lead_score, "temperature": result.lead_temperature},
                                   ensure_ascii=False, sort_keys=True), update_modified=False)
    return {
        "lead_score": f"{result.lead_score:.2f}",
        "lead_temperature": result.lead_temperature,
        "lead_lan": result.lead_lan,
        "source": result.source,
    }


def recompute_conversation_metrics(conversation: str) -> ScoreResult:
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    bundle = get_conversation_policy(convo)
    policy = bundle.section("lead_scoring_policy") if bundle else {}
    validate_scoring_policy(policy)
    history = []
    if policy.get("enabled"):
        limit = policy.get("history_limit", 0)
        # Read all messages by default; a configured limit selects the most recent.
        offset = 0
        while True:
            size = min(500, limit - offset) if limit else 500
            rows = safe_ai_get_all(
                "Chat Message", filters={"conversation": conversation},
                fields=["name", "direction", "body", "creation", "content_type", "media_url",
                        "attachment_file", "scoring_media_text"],
                order_by="creation desc, name desc", limit_start=offset, limit_page_length=size,
            )
            history.extend(rows)
            offset += len(rows)
            if len(rows) < size or (limit and offset >= limit):
                break
        history.reverse()
    return _policy_score(convo, history, bundle=bundle)


def sync_to_conversation(conversation: str, result: ScoreResult) -> None:
    """Persist scoring only when at least one derived value changed."""
    assert_ai_doctype_permission("Chat Conversation", "write")
    frappe.db.sql(
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

    assert_ai_doctype_permission(target_dt, "write")
    # Field names come only from the fixed derived-field allowlist above.
    assignments = ", ".join(f"`{field}`=%s" for field in updates)
    changed = " OR ".join(f"NOT (`{field}` <=> %s)" for field in updates)
    values = tuple(updates.values())
    frappe.db.sql(
        f"UPDATE `tabCRM Lead` SET {assignments} WHERE name=%s AND ({changed})",
        (*values, crm_lead, *values),
    )


def _policy_score(convo, history: List[Dict], *, bundle=None) -> ScoreResult:
    bundle = bundle or get_conversation_policy(convo)
    policy = bundle.section("lead_scoring_policy") if bundle else {}
    validate_scoring_policy(policy)
    if not policy.get("enabled"):
        return ScoreResult(
            lead_score=float(getattr(convo, "lead_score", 0) or 0),
            lead_temperature=str(getattr(convo, "lead_temperature", "") or ""),
            lead_lan=str(getattr(convo, "lead_lan", "") or ""), source="policy_disabled",
        )
    inbound = [h for h in history if h.get("direction") == "Inbound" and str(h.get("body") or "").strip()]
    latest_text = inbound[-1]["body"] if inbound else ""
    lang = resolve_language_from_history(
        str(latest_text or ""), history, channel_account=getattr(convo, "channel_account", None)
    )
    result = _policy_rule_score(convo, history, str(lang.get("label") or "").strip(), policy)
    result.details.update({"policy": bundle.name, "policy_version": bundle.version,
        "policy_fingerprint": hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()})
    return result


def _policy_rule_score(convo, history: List[Dict], lead_lan: str, policy: Dict) -> ScoreResult:
    score, label, details = evaluate(policy, history, unread_count=convo.unread_count,
        priority=convo.priority, stopped=bool(getattr(convo, "conversation_stopped", 0)))
    return ScoreResult(score, label, lead_lan, "policy", details)


def _score_to_temperature(score: float, policy: Dict) -> str:
    return temperature(score, policy)


def preview_conversation_score(conversation):
    """Read-only preview for bench/admin scripts; never sends messages or writes scores."""
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    result = recompute_conversation_metrics(conversation)
    return {"conversation": str(conversation), "previous_score": convo.lead_score,
            "previous_temperature": convo.lead_temperature, "score": result.lead_score,
            "temperature": result.lead_temperature, "source": result.source, "details": result.details}


def rescore_batch(conversations, dry_run=True):
    """Explicit, bounded backfill. Preview is the default; no automatic bulk rollout."""
    if not isinstance(conversations, list) or not 1 <= len(conversations) <= 100:
        raise ValueError("Supply a list of 1 to 100 conversation IDs")
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a boolean")
    if dry_run:
        return [preview_conversation_score(str(name)) for name in conversations]
    from wa_chat_hub.lead_ai import _schedule
    for name in conversations:
        assert_ai_doctype_permission("Chat Conversation", "write")
        safe_ai_get_doc("Chat Conversation", str(name))
        _schedule(str(name))
    return {"scheduled": len(conversations)}


def record_media_scoring_text(message_name, text):
    """Store derived evidence, never a model-selected score or temperature."""
    text = str(text or "").strip()
    if not text:
        return
    assert_ai_doctype_permission("Chat Message", "write")
    doc = safe_ai_get_doc("Chat Message", message_name)
    if getattr(doc, "scoring_media_text", None) == text[:16000]:
        return
    frappe.db.set_value("Chat Message", message_name, "scoring_media_text", text[:16000], update_modified=False)
    from wa_chat_hub.lead_ai import _schedule
    _schedule(doc.conversation)


def needs_media_scoring_text(convo):
    """Only add analysis for unlinked chats when the policy requires its evidence."""
    bundle = get_conversation_policy(convo)
    policy = bundle.section("lead_scoring_policy") if bundle else {}
    media = policy.get("disease_media_scoring") or {}
    return bool(policy.get("enabled") and media.get("enabled") and media.get("require_disease_related_media"))
