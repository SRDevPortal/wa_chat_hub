from __future__ import annotations

import frappe
from frappe.model.document import Document


class WAAIDocTypePermission(Document):
    def validate(self):
        self.doctype_name = (self.doctype_name or "").strip()
        if not self.doctype_name:
            return
        if not frappe.db.exists("DocType", self.doctype_name):
            frappe.throw(f"DocType {self.doctype_name} does not exist.")


WAAIDoctypePermission = WAAIDocTypePermission
