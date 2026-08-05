from __future__ import annotations

import hashlib

import frappe
from frappe import _
from frappe.model.document import Document


class WAAIIntentRoute(Document):
    def validate(self):
        if self.target_type == "Agent Profile" and not self.agent_profile:
            frappe.throw(_("Agent Profile is required when Target Type is Agent Profile."))
        if self.target_type != "Agent Profile":
            self.agent_profile = None
        if not self.is_active:
            self.scope_key = None
            return

        parts = (
            self.intent or "*",
            self.data_scope or "*",
            self.channel_account or "*",
            self.party_type or "*",
            self.identity_status or "*",
        )
        raw_scope = "|".join(str(part).strip().lower() for part in parts)
        self.scope_key = hashlib.sha256(raw_scope.encode("utf-8")).hexdigest()

        duplicate = frappe.db.exists(
            "WA AI Intent Route",
            {"scope_key": self.scope_key, "name": ["!=", self.name or self.route_name]},
        )
        if duplicate:
            frappe.throw(_("Another intent route already uses the same matching scope."))
