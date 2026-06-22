from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List

import frappe
import requests

from wa_chat_hub.ai.conversation_stop import is_conversation_stopped
from wa_chat_hub.ai.language import resolve_language_from_history
from wa_chat_hub.db_retry import with_db_lock_retry
from wa_chat_hub.prompts import get_conversation_crm_lead


@dataclass
class ScoreResult:
    lead_score: float
    lead_temperature: str
    lead_lan: str
    source: str


def score_and_sync_conversation(conversation: str) -> Dict[str, str]:
    result = recompute_conversation_metrics(conversation)
    sync_to_linked_lead(conversation, result)
    return {
        "lead_score": f"{result.lead_score:.2f}",
        "lead_temperature": result.lead_temperature,
        "lead_lan": result.lead_lan,
        "source": result.source,
    }


def recompute_conversation_metrics(conversation: str) -> ScoreResult:
    convo = frappe.get_doc("Chat Conversation", conversation)
    if is_conversation_stopped(conversation):
        score_result = ScoreResult(
            lead_score=0,
            lead_temperature="Cold",
            lead_lan=convo.lead_lan or "English",
            source="conversation_stopped",
        )
        with_db_lock_retry(
            "conversation_stopped_score_update",
            lambda: frappe.db.set_value(
                "Chat Conversation",
                conversation,
                {
                    "lead_score": score_result.lead_score,
                    "lead_temperature": score_result.lead_temperature,
                    "lead_lan": score_result.lead_lan,
                },
                update_modified=False,
            ),
        )
        sync_to_linked_lead(conversation, score_result)
        return score_result

    history = frappe.get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["direction", "body", "creation"],
        order_by="creation asc",
        limit_page_length=40,
    )
    score_result = _ai_score(convo, history)

    with_db_lock_retry(
        "conversation_lead_score_update",
        lambda: frappe.db.set_value(
            "Chat Conversation",
            conversation,
            {
                "lead_score": score_result.lead_score,
                "lead_temperature": score_result.lead_temperature,
                "lead_lan": score_result.lead_lan,
            },
            update_modified=False,
        ),
    )
    return score_result


def sync_to_linked_lead(conversation: str, result: ScoreResult | None = None) -> None:
    convo = frappe.get_doc("Chat Conversation", conversation)
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
    if not frappe.db.exists(target_dt, crm_lead):
        return

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

    frappe.db.set_value(
        target_dt,
        crm_lead,
        updates,
        update_modified=False,
    )


def _ai_score(convo, history: List[Dict]) -> ScoreResult:
    inbound = [h for h in history if h.get("direction") == "Inbound" and str(h.get("body") or "").strip()]
    latest_text = inbound[-1]["body"] if inbound else ""
    lang = resolve_language_from_history(str(latest_text or ""), history)
    lead_lan = (lang.get("label") or "English").strip()

    providers = _load_active_providers()
    if not providers:
        return _heuristic_score(convo, history, lead_lan)

    prompt = _build_scoring_prompt(convo, history, lead_lan)
    for provider in providers:
        try:
            score = _call_score_provider(provider, prompt)
            if score is None:
                continue
            score = _clamp(score, 0, 100)
            return ScoreResult(
                lead_score=score,
                lead_temperature=_score_to_temperature(score),
                lead_lan=lead_lan,
                source=f"ai:{provider['name']}",
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"Lead Scoring Provider Failed: {provider['name']}")

    return _heuristic_score(convo, history, lead_lan)


def _build_scoring_prompt(convo, history: List[Dict], lead_lan: str) -> str:
    trimmed = []
    for row in history[-12:]:
        text = str(row.get("body") or "").strip()
        if not text:
            continue
        trimmed.append(f"{row.get('direction')}: {text[:240]}")

    transcript = "\n".join(trimmed) or "No usable transcript."
    return (
        "You are a CRM lead scoring assistant.\n"
        "Score this WhatsApp conversation from 0 to 100 for conversion readiness.\n"
        "Return only JSON: {\"score\": number}.\n"
        "Signals: buying intent, urgency, appointment intent, detailed responses, follow-up behavior.\n"
        "Conversation metadata:\n"
        f"- Priority: {convo.priority}\n"
        f"- Status: {convo.status}\n"
        f"- Unread count: {convo.unread_count}\n"
        f"- Detected language: {lead_lan}\n\n"
        f"Transcript:\n{transcript}"
    )


def _load_active_providers() -> List[Dict]:
    providers = frappe.get_all(
        "WA LLM Provider",
        filters={"is_active": 1},
        fields=["name", "provider_type", "model_name", "base_url"],
        order_by="priority asc",
    )
    result = []
    for row in providers:
        doc = frappe.get_doc("WA LLM Provider", row.name)
        api_key = doc.get_password("api_key")
        if not api_key:
            continue
        result.append(
            {
                "name": row.name,
                "provider_type": row.provider_type,
                "model_name": row.model_name,
                "base_url": row.base_url,
                "api_key": api_key,
            }
        )
    return result


def _call_score_provider(provider: Dict, prompt: str) -> float | None:
    url = provider.get("base_url") or "https://api.openai.com/v1/chat/completions"
    if provider.get("provider_type") == "Gemini" and not provider.get("base_url"):
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    if url.endswith("/") and "chat/completions" not in url:
        url = f"{url}chat/completions"

    payload = {
        "model": provider.get("model_name"),
        "messages": [
            {"role": "system", "content": "Return strict JSON with key score only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {provider.get('api_key')}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=20,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"].get("content", "").strip()
    if not content:
        return None

    try:
        parsed = json.loads(content)
        return float(parsed.get("score"))
    except Exception:
        digits = "".join(ch for ch in content if ch.isdigit() or ch == ".")
        return float(digits) if digits else None


def _heuristic_score(convo, history: List[Dict], lead_lan: str) -> ScoreResult:
    score = 20.0
    inbound_count = len([h for h in history if h.get("direction") == "Inbound" and str(h.get("body") or "").strip()])
    score += min(30, inbound_count * 3)
    score += min(15, int(convo.unread_count or 0) * 2)
    score += {"Low": 0, "Medium": 5, "High": 12, "Urgent": 20}.get(convo.priority, 0)

    joined = " ".join([str(h.get("body") or "") for h in history]).lower()
    hot_terms = ["price", "cost", "book", "appointment", "consult", "today", "urgent", "buy", "payment"]
    warm_terms = ["interested", "details", "plan", "treatment", "package"]
    score += sum(4 for term in hot_terms if term in joined)
    score += sum(2 for term in warm_terms if term in joined)

    score = _clamp(score, 0, 100)
    return ScoreResult(
        lead_score=score,
        lead_temperature=_score_to_temperature(score),
        lead_lan=lead_lan,
        source="heuristic",
    )


def _score_to_temperature(score: float) -> str:
    if score >= 70:
        return "Hot"
    if score >= 40:
        return "Warm"
    return "Cold"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))
