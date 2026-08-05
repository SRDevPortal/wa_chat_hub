from __future__ import annotations

import json
from pathlib import Path

import frappe


SEED_FILES = ("default_policy_bundle.json", "default_mcp_endpoints.json", "default_ai_routing.json")
SEED_ORDER = (
    "WA AI Policy Bundle",
    "WA MCP Tool Endpoint",
    "WA AI Intent",
    "WA AI Workflow",
    "WA AI Intent Route",
)
SEED_VERSION_FIELDS = {
    "WA AI Policy Bundle": "policy_version",
    "WA MCP Tool Endpoint": "configuration_version",
    "WA AI Workflow": "workflow_version",
    "WA AI Intent Route": "configuration_version",
}
JSON_FIELDS = (
    "parameters_schema",
    "execution_config",
    "fallback_match",
    "definition",
    "blocked_replies",
    "identity_policy",
    "party_routing_policy",
    "language_policy",
    "reply_templates",
    "patient_creation_policy",
    "lead_scoring_policy",
    "media_policy",
    "provider_policy",
    "runtime_policy",
    "permission_policy",
)


def seed_default_ai_routing() -> dict[str, int]:
    """Insert bootstrap records and upgrade explicitly versioned seed records."""
    data: dict[str, list[dict]] = {}
    for filename in SEED_FILES:
        path = Path(frappe.get_app_path("wa_chat_hub", "config", filename))
        source = json.loads(path.read_text(encoding="utf-8"))
        for doctype, records in source.items():
            data.setdefault(doctype, []).extend(records or [])
    result: dict[str, int] = {}
    for doctype in SEED_ORDER:
        if not frappe.db.exists("DocType", doctype):
            result[doctype] = 0
            continue
        changed = 0
        for values in data.get(doctype) or []:
            name_field = {
                "WA AI Policy Bundle": "policy_name",
                "WA MCP Tool Endpoint": "tool_name",
                "WA AI Intent": "intent_name",
                "WA AI Workflow": "workflow_name",
                "WA AI Intent Route": "route_name",
            }[doctype]
            name = str(values.get(name_field) or "").strip()
            if not name:
                continue
            doc_values = _serialized_seed_values(values)
            if not frappe.db.exists(doctype, name):
                frappe.get_doc({"doctype": doctype, **doc_values}).insert(ignore_permissions=True)
                changed += 1
                continue
            version_field = SEED_VERSION_FIELDS.get(doctype)
            seed_version = int(values.get(version_field) or 0) if version_field else 0
            if not version_field or seed_version <= 0:
                continue
            current_version = int(frappe.db.get_value(doctype, name, version_field) or 0)
            if current_version >= seed_version:
                continue
            doc = frappe.get_doc(doctype, name)
            for fieldname, value in doc_values.items():
                if fieldname not in {name_field, "doctype"}:
                    doc.set(fieldname, value)
            doc.save(ignore_permissions=True)
            changed += 1
        result[doctype] = changed
    return result


def _serialized_seed_values(values: dict) -> dict:
    doc_values = dict(values)
    for fieldname in JSON_FIELDS:
        if isinstance(doc_values.get(fieldname), (dict, list)):
            doc_values[fieldname] = json.dumps(
                doc_values[fieldname], ensure_ascii=False, indent=2
            )
    return doc_values


def ensure_default_policy_assignment() -> int:
    """Assign the migration policy only to existing accounts that have no policy."""
    if not frappe.db.exists("DocType", "WA AI Policy Bundle"):
        return 0
    account_meta = frappe.get_meta("Chat Channel Account")
    if not account_meta.has_field("ai_policy_bundle"):
        return 0
    rows = frappe.get_all(
        "WA AI Policy Bundle",
        filters={"is_active": 1, "is_default": 1},
        fields=["name"],
        limit_start=0,
        limit_page_length=2,
    )
    if not rows:
        return 0
    if len(rows) > 1:
        frappe.throw("Only one active WA AI Policy Bundle may be marked as migration default.")
    policy_name = rows[0].name
    table = "`tabChat Channel Account`"
    frappe.db.sql(
        f"UPDATE {table} SET ai_policy_bundle = %s WHERE ai_policy_bundle IS NULL",
        (policy_name,),
    )
    updated = int(getattr(frappe.db._cursor, "rowcount", 0) or 0)
    frappe.db.sql(
        f"UPDATE {table} SET ai_policy_bundle = %s WHERE ai_policy_bundle = %s",
        (policy_name, ""),
    )
    updated += int(getattr(frappe.db._cursor, "rowcount", 0) or 0)
    frappe.clear_cache(doctype="Chat Channel Account")
    return updated


def ensure_default_route_blocked_replies() -> int:
    """Backfill missing language maps without overwriting administrator changes."""
    doctype = "WA AI Intent Route"
    if not frappe.db.exists("DocType", doctype):
        return 0
    meta = frappe.get_meta(doctype)
    if not meta.has_field("blocked_replies"):
        return 0

    path = Path(frappe.get_app_path("wa_chat_hub", "config", "default_ai_routing.json"))
    source = json.loads(path.read_text(encoding="utf-8"))
    updated = 0
    for values in source.get(doctype) or []:
        name = str(values.get("route_name") or "").strip()
        replies = values.get("blocked_replies")
        if (
            not name
            or not isinstance(replies, dict)
            or not replies
            or not frappe.db.exists(doctype, name)
            or frappe.db.get_value(doctype, name, "blocked_replies")
        ):
            continue
        frappe.db.set_value(
            doctype,
            name,
            "blocked_replies",
            json.dumps(replies, ensure_ascii=False, indent=2),
            update_modified=False,
        )
        updated += 1
    return updated
