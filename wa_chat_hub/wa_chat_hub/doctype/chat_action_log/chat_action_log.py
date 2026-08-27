from __future__ import annotations

from typing import Any, Optional

import frappe
from frappe.model.document import Document


class ChatActionLog(Document):
    pass


def log_chat_action(
    action_type: str,
    action_name: str,
    status: str = "Success",
    *,
    conversation: Optional[str] = None,
    channel_account: Optional[str] = None,
    action_source: str = "System",
    actor: Optional[str] = None,
    reference_doctype: Optional[str] = None,
    reference_name: Optional[str] = None,
    details: Optional[str] = None,
    request_json: Optional[str] = None,
    response_json: Optional[str] = None,
) -> Optional[str]:
    """Append an audit row when Chat Action Log is installed."""
    if not frappe.db.exists("DocType", "Chat Action Log"):
        return None

    doc = frappe.get_doc(
        {
            "doctype": "Chat Action Log",
            "conversation": conversation,
            "channel_account": channel_account,
            "action_source": action_source,
            "action_type": action_type,
            "action_name": action_name,
            "status": status,
            "actor": actor or frappe.session.user if getattr(frappe.session, "user", None) else None,
            "reference_doctype": reference_doctype,
            "reference_name": reference_name,
            "details": details,
            "request_json": request_json,
            "response_json": response_json,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name
