"""Append derived media notes without revalidating unrelated lead fields."""
from __future__ import annotations

import frappe

from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_get_doc,
    safe_ai_set_value,
)


def merge_note(existing: str, note: str, limit: int = 6000) -> str:
    existing, note = str(existing or "").strip(), str(note or "").strip()
    merged = f"{existing}\n\n{note}".strip()
    if len(merged) <= limit:
        return merged
    suffix = "\n... [truncated]"
    if len(note) >= limit:
        return note[:limit - len(suffix)] + suffix
    prefix = "... [older notes truncated]\n\n"
    budget = limit - len(prefix) - len(note) - 2
    if budget <= 0:
        return note
    return prefix + existing[-budget:] + "\n\n" + note


def append_lead_note(doctype: str, name: str, note: str, notes_field: str | None) -> None:
    # Check all permissions before writing either the note or its audit comment.
    assert_ai_doctype_permission(doctype, "read")
    assert_ai_doctype_permission("Comment", "write")
    if notes_field:
        assert_ai_doctype_permission(doctype, "write")
        if notes_field not in {"sr_lead_notes", "notes"}:
            raise ValueError("Unsupported lead notes field")
        table = "tab" + doctype.replace("`", "``")
        rows = frappe.db.sql(
            f"SELECT `{notes_field}` FROM `{table}` WHERE name=%s FOR UPDATE",
            (name,),
        )
        if not rows:
            raise frappe.DoesNotExistError(doctype)
        # The locking read sees the latest committed notes, even under REPEATABLE
        # READ. Keep this write and the comment in the caller's transaction.
        safe_ai_set_value(doctype, name, {notes_field: merge_note(rows[0][0], note)})
    safe_ai_get_doc(doctype, name).add_comment("Comment", note)
