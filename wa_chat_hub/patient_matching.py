"""Bounded patient matching shared by inbound routing and identity refreshes."""
from __future__ import annotations

import frappe
from uuid import uuid4


def patient_context_allowed(patient, identity_status, policy=None) -> bool:
    statuses = policy.section("identity_policy").get("statuses", {}) if policy else {}
    return bool(patient) and identity_status in {
        statuses.get("matched") or "Matched", statuses.get("verified") or "Verified"
    }


def _cache_key(phone):
    from wa_chat_hub.services import normalize_phone

    # The existing indexed lookup compares the final ten digits as well.
    return f"wa_patient_match:v1:{normalize_phone(phone)[-10:]}"


def find_patient_matches(phone, phone_fields):
    from wa_chat_hub.services import _find_indexed_phone_match_names, assert_ai_doctype_permission

    assert_ai_doctype_permission("Patient", "read")
    cache = frappe.cache()
    base_key = _cache_key(phone)
    revision = cache.get_value(base_key + ":revision", expires=True) or "0"
    key = f"{base_key}:matches:{revision}"
    fields = tuple(sorted(phone_fields))
    cached = cache.get_value(key, expires=True)
    if isinstance(cached, dict) and tuple(cached.get("fields", [])) == fields:
        return set(cached["patients"])
    matches = _find_indexed_phone_match_names("Patient", list(fields), phone, limit=2)
    # Both matches and misses expire; Patient hooks invalidate immediately.
    # Never publish a Patient created in an uncommitted transaction to another
    # worker. This is a transaction callback, not a background job.
    frappe.db.after_commit.add(
        lambda: cache.set_value(key, {"fields": fields, "patients": sorted(matches)}, expires_in_sec=300)
    )
    return matches


def invalidate_patient_matches(doc, method=None):
    """Invalidate old and new phone keys only on creation/deletion/phone edits."""
    from wa_chat_hub.services import PHONE_INDEX_FIELD_BY_SOURCE, PHONE_CANONICAL_INDEX_FIELDS

    fields = set(PHONE_INDEX_FIELD_BY_SOURCE) | set(PHONE_CANONICAL_INDEX_FIELDS)
    previous = doc.get_doc_before_save()
    if previous and method != "on_trash" and all(doc.get(f) == previous.get(f) for f in fields):
        return
    keys = {
        _cache_key(record.get(field))
        for record in (previous, doc) if record
        for field in fields if record.get(field)
    }

    def invalidate():
        for key in keys:
            # Versioned keys also isolate a slow reader that caches the old DB
            # result after invalidation. Old result keys expire by themselves.
            frappe.cache().set_value(key + ":revision", uuid4().hex, expires_in_sec=86400)

    invalidate()
    # A concurrent reader can repopulate a key before this transaction commits.
    frappe.db.after_commit.add(invalidate)
    frappe.db.after_rollback.add(invalidate)
