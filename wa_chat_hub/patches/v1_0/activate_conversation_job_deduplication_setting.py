from __future__ import annotations

import frappe

from wa_chat_hub.performance_flags import (
    CONVERSATION_JOB_DEDUPLICATION_ACTIVE_FIELD,
    CONVERSATION_JOB_DEDUPLICATION_FIELD,
)


def execute() -> None:
    """Make the stored setting match the existing effective runtime behavior."""
    if not frappe.db.exists("DocType", "WA Chat Hub Settings"):
        return

    meta = frappe.get_meta("WA Chat Hub Settings")
    if not meta.has_field(CONVERSATION_JOB_DEDUPLICATION_FIELD):
        return
    if not meta.has_field(CONVERSATION_JOB_DEDUPLICATION_ACTIVE_FIELD):
        return

    frappe.db.set_single_value(
        "WA Chat Hub Settings",
        CONVERSATION_JOB_DEDUPLICATION_FIELD,
        1,
    )
    frappe.db.set_single_value(
        "WA Chat Hub Settings",
        CONVERSATION_JOB_DEDUPLICATION_ACTIVE_FIELD,
        1,
    )
    frappe.clear_cache(doctype="WA Chat Hub Settings")
