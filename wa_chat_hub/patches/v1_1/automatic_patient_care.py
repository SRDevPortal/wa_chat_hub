"""Retire number challenges without rewriting chat history or unrelated settings."""
from __future__ import annotations

import json

import frappe

def policy_updates(doc) -> dict:
    identity = json.loads(doc.get("identity_policy") or "{}")
    routing = json.loads(doc.get("party_routing_policy") or "{}")
    replies = json.loads(doc.get("reply_templates") or "{}")
    identity.update(
        validity_hours=0, verification_tool_name="",
        allow_supplied_registered_number=False, allow_current_chat_number_claim=False,
    )
    statuses = identity.get("statuses") or {}
    party = (routing.get("party_type_by_reference_doctype") or {}).get("Patient", "Patient")
    mappings = routing.setdefault("agent_type_by_party_status", {}).setdefault(party, {})
    mappings[statuses.get("matched") or "Matched"] = "Patient"
    mappings[statuses.get("verified") or "Verified"] = "Patient"
    for status, agent_type in list(mappings.items()):
        if agent_type == "Patient Verification":
            mappings[status] = "General"
    return {
        "identity_policy": json.dumps(identity, ensure_ascii=False, indent=2),
        "party_routing_policy": json.dumps(routing, ensure_ascii=False, indent=2),
        "reply_templates": json.dumps(replies, ensure_ascii=False, indent=2),
    }


def _records(doctype, filters=None):
    if not frappe.db.exists("DocType", doctype):
        return
    last = ""
    while True:
        names = frappe.get_all(
            doctype, filters={**(filters or {}), "name": [">", last]},
            pluck="name", order_by="name asc", limit_page_length=100,
        )
        if not names:
            return
        for name in names:
            yield frappe.get_doc(doctype, name)
        last = names[-1]


def _save_changes(doc, values):
    changed = False
    for key, value in values.items():
        if doc.get(key) != value:
            doc.set(key, value)
            changed = True
    if changed:
        doc.save(ignore_permissions=True)


def execute():
    verification_profiles = set()
    patient_profile = frappe.db.get_value(
        "WA AI Agent Profile", {"agent_type": "Patient", "is_default": 1, "is_active": 1}, "name"
    )
    for doc in _records("WA AI Policy Bundle"):
        _save_changes(doc, policy_updates(doc))
    for doc in _records("WA AI Agent Profile"):
        updates = {}
        tools_changed = False
        if doc.agent_type == "Patient Verification":
            verification_profiles.add(doc.name)
            updates["is_active"] = 0
        if doc.agent_type == "Patient":
            updates["require_verified_identity"] = 0
        # Deactivate legacy tool rows, retaining their history and other tools.
        for row in doc.get("allowed_tools") or []:
            if row.mcp_tool == "verify_patient_identity" and row.is_active:
                row.is_active = 0
                tools_changed = True
        if tools_changed:
            for key, value in updates.items():
                doc.set(key, value)
            doc.save(ignore_permissions=True)
        else:
            _save_changes(doc, updates)
    for doc in _records("WA AI Intent Route"):
        if doc.agent_profile not in verification_profiles:
            continue
        if doc.identity_status in {"Matched", "Verified"} and patient_profile:
            updates = {"agent_profile": patient_profile, "blocked_reply": "", "blocked_replies": "{}"}
        else:
            updates = {
                "target_type": "Channel Account Default", "agent_profile": None,
            }
        _save_changes(doc, updates)
    for doctype, name in (
        ("WA AI Intent", "identity_verification"),
        ("WA MCP Tool Endpoint", "verify_patient_identity"),
    ):
        if frappe.db.exists(doctype, name):
            _save_changes(frappe.get_doc(doctype, name), {"is_active": 0})
    # Chat links are refreshed on inbound traffic. No conversation-table backfill,
    # scheduled task, outbound messages, or new worker jobs are needed.
