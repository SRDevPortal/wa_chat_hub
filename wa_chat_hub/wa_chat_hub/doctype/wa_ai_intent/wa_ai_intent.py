from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


ALLOWED_MATCH_KEYS = {"full_text_any", "contains_any", "contains_all", "excludes_any"}


class WAAIIntent(Document):
    def validate(self):
        key = str(self.intent_name or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", key):
            frappe.throw(_("Intent Name must be a lowercase identifier using letters, numbers, and underscores."))
        self.intent_name = key

        threshold = flt(self.confidence_threshold)
        if threshold < 0 or threshold > 1:
            frappe.throw(_("Confidence Threshold must be between 0 and 1."))

        _validate_fallback_match(self.fallback_match)
        if self.is_active and self.is_default_fallback:
            filters = {
                "is_active": 1,
                "is_default_fallback": 1,
                "name": ["!=", self.name or key],
            }
            if frappe.db.exists("WA AI Intent", filters):
                frappe.throw(_("Only one active WA AI Intent can be the Default Fallback."))


def _validate_fallback_match(raw) -> None:
    if not raw:
        return
    try:
        value = frappe.parse_json(raw)
    except Exception:
        frappe.throw(_("Fallback Match must be valid JSON."))
    if not isinstance(value, dict):
        frappe.throw(_("Fallback Match must be a JSON object."))
    unknown = set(value) - ALLOWED_MATCH_KEYS
    if unknown:
        frappe.throw(_("Unsupported Fallback Match keys: {0}").format(", ".join(sorted(unknown))))

    for key, items in value.items():
        if key == "contains_all":
            if not isinstance(items, list) or len(items) > 30:
                frappe.throw(_("contains_all must be a list with at most 30 phrase groups."))
            groups = items
        else:
            groups = [items]
        for group in groups:
            if not isinstance(group, list) or len(group) > 50:
                frappe.throw(_("Fallback match phrases must be arrays with at most 50 items."))
            for phrase in group:
                if not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 160:
                    frappe.throw(_("Fallback match phrases must be non-empty strings of at most 160 characters."))
