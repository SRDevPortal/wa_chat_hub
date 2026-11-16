"""Minimal registered-name context for automatically matched patients."""
from __future__ import annotations

import json
from uuid import uuid4

import frappe

from wa_chat_hub.security import assert_ai_doctype_permission, safe_ai_get_value

def get_patient_display_name(patient: str) -> str:
    if not patient:
        return ""
    assert_ai_doctype_permission("Patient", "read")
    cache = frappe.cache()
    base = f"wa_patient_name:v1:{patient}"
    revision = cache.get_value(base + ":revision", expires=True) or "0"
    key = f"{base}:value:{revision}"
    cached = cache.get_value(key, expires=True)
    if isinstance(cached, str):
        return cached
    name = str(safe_ai_get_value("Patient", patient, "patient_name") or "").strip()
    frappe.db.after_commit.add(lambda: cache.set_value(key, name, expires_in_sec=3600))
    return name


def build_patient_name_context(route) -> str:
    if not route.patient_access_allowed:
        return ""
    name = get_patient_display_name(route.patient)
    if not name:
        return ""
    return json.dumps({"patient_name": name}, ensure_ascii=False)


def invalidate_patient_display_name(doc, method=None):
    previous = doc.get_doc_before_save()
    if previous and method != "on_trash" and previous.get("patient_name") == doc.get("patient_name"):
        return

    def invalidate():
        frappe.cache().set_value(
            f"wa_patient_name:v1:{doc.name}:revision", uuid4().hex, expires_in_sec=86400
        )

    invalidate()
    frappe.db.after_commit.add(invalidate)
    frappe.db.after_rollback.add(invalidate)
