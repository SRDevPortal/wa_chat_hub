from __future__ import annotations

from typing import Any

import frappe


AUDITED_EVENT_TYPES = frozenset(
    {
        "Assignment",
        "Status",
        "Priority",
        "Stop",
        "Reopen",
        "Lead Link",
        "Patient Link",
        "Manual Score Override",
        "Other",
    }
)


def _audit_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return frappe.as_json(value)
    return str(value)


def record_conversation_change(
    conversation: str,
    event_type: str,
    *,
    fieldname: str | None = None,
    old_value: Any = None,
    new_value: Any = None,
    actor: str | None = None,
    source: str | None = None,
    source_message: str | None = None,
    request_id: str | None = None,
) -> str | None:
    """Append one compact audit event for a meaningful manual/business change."""
    if not conversation or event_type not in AUDITED_EVENT_TYPES:
        return None
    if not frappe.db.exists("DocType", "WA Conversation Audit Event"):
        return None

    event = frappe.get_doc(
        {
            "doctype": "WA Conversation Audit Event",
            "conversation": conversation,
            "event_type": event_type,
            "fieldname": fieldname,
            "old_value": _audit_value(old_value),
            "new_value": _audit_value(new_value),
            "actor": actor or frappe.session.user,
            "source": source,
            "source_message": source_message,
            "request_id": request_id or getattr(frappe.local, "request_id", None),
        }
    )
    event.insert(ignore_permissions=True)
    return event.name
