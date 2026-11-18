"""Compatibility entry points for policy-driven lead scoring."""

from __future__ import annotations

from typing import Any

import frappe
from uuid import uuid4


def _schedule(conversation: str) -> bool:
    conversation = str(conversation or "").strip()
    if not conversation:
        return False
    frappe.enqueue(
        "wa_chat_hub.ai.lead_scoring.score_and_sync_conversation_job",
        queue="short",
        timeout=90,
        conversation=conversation,
        enqueue_after_commit=True,
        job_id=f"wa_policy_lead_score_{conversation}_{uuid4().hex}",
        deduplicate=False,
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


SCORING_INPUT_FIELDS = {"linked_crm_lead", "linked_reference_doctype", "linked_reference_name",
                        "channel_account", "priority", "unread_count", "conversation_stopped"}


def on_conversation_update(doc, method=None):
    if any(doc.has_value_changed(field) for field in SCORING_INPUT_FIELDS):
        _schedule(doc.name)


def on_message_update(doc, method=None):
    if getattr(getattr(doc, "flags", None), "in_insert", False):
        return  # after_insert already scheduled this message
    if any(doc.has_value_changed(field) for field in
           ("body", "content_type", "media_url", "attachment_file", "scoring_media_text")):
        _schedule(doc.conversation)
