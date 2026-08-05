"""Compatibility entry points for policy-driven lead scoring."""

from __future__ import annotations

from typing import Any

import frappe


def _schedule(conversation: str) -> bool:
    conversation = str(conversation or "").strip()
    if not conversation:
        return False
    frappe.enqueue(
        "wa_chat_hub.ai.lead_scoring.score_and_sync_conversation",
        queue="short",
        conversation=conversation,
        enqueue_after_commit=True,
        job_id=f"wa_policy_lead_score_{conversation}",
        deduplicate=True,
    )
    return True


def on_chat_message_after_insert(doc, method=None):
    try:
        _schedule(getattr(doc, "conversation", None))
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Policy Lead Scoring Enqueue Failed")


def auto_update_lead_from_conversation(
    lead_name: str,
    conversation: str | None = None,
) -> dict[str, Any]:
    """Retained API; all scoring behavior now comes from the assigned policy."""
    del lead_name
    scheduled = _schedule(str(conversation or ""))
    return {
        "updated": False,
        "scheduled": scheduled,
        "reason": "scheduled" if scheduled else "conversation_missing",
    }
