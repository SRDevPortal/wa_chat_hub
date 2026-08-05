import frappe
from frappe import _
from frappe.model.document import Document


class WAAppUpdateLog(Document):
    def validate(self):
        if not self.is_new():
            frappe.throw(_("App update logs are immutable."))

    def on_trash(self):
        frappe.throw(_("App update logs are retained as a permanent audit trail."))
