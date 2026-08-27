from __future__ import annotations

import frappe

from wa_chat_hub.security import safe_ai_get_all, safe_ai_get_doc


def get_or_create_settings():
    name = frappe.db.get_single_value("WA Chat Hub Settings", "name")
    if not name:
        doc = frappe.get_doc({"doctype": "WA Chat Hub Settings"})
        doc.insert(ignore_permissions=True)
        return doc
    return frappe.get_single("WA Chat Hub Settings")


def get_active_mcp_servers():
    return []


def get_active_knowledge_base(
    context: str | None = None,
    department: str | None = None,
    channel_account: str | None = None,
    allowed_names: set[str] | None = None,
):
    fields = _knowledge_base_fields()
    if context:
        doc = safe_ai_get_doc("WA Channel Context", context)
        names = [row.knowledge_base for row in doc.get("knowledge_bases") or [] if row.is_active and row.knowledge_base]
        if not names:
            return []
        rows = safe_ai_get_all(
            "WA AI Knowledge Base",
            filters={"name": ["in", names], "is_active": 1},
            fields=fields,
            order_by="priority desc, modified desc",
        )
        return _filter_knowledge_rows(rows, department=department, channel_account=channel_account)

    filters = {"is_active": 1}
    if allowed_names:
        filters["name"] = ["in", sorted(allowed_names)]
    if channel_account and "chat_channel_account" in fields:
        filters["chat_channel_account"] = ["in", ["", channel_account]]
    rows = safe_ai_get_all(
        "WA AI Knowledge Base",
        filters=filters,
        fields=fields,
        order_by="priority desc, modified desc",
    )
    return _filter_knowledge_rows(rows, department=department, channel_account=channel_account)


def _knowledge_base_fields() -> list[str]:
    fields = ["name", "kb_label", "kb_type", "source_path", "source_url", "department", "priority", "content"]
    if frappe.get_meta("WA AI Knowledge Base").has_field("medical_department"):
        fields.insert(6, "medical_department")
    if frappe.get_meta("WA AI Knowledge Base").has_field("chat_channel_account"):
        fields.insert(3, "chat_channel_account")
    return fields


def _filter_knowledge_rows(
    rows,
    *,
    department: str | None = None,
    channel_account: str | None = None,
):
    result = []
    for row in rows:
        row_department = row.get("medical_department") or row.get("department")
        row_channel_account = row.get("chat_channel_account")
        if department and row_department and row_department != department:
            continue
        if channel_account and row_channel_account and row_channel_account != channel_account:
            continue
        result.append(row)
    return result
