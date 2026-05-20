import frappe
from frappe import _
from frappe.model.document import Document


class WAChannelContext(Document):
    def validate(self):
        if self.is_active:
            duplicate = frappe.db.get_value(
                "WA Channel Context",
                {
                    "name": ["!=", self.name],
                    "pipeline": self.pipeline,
                    "is_active": 1,
                },
                "name",
            )
            if duplicate:
                frappe.throw(_("Pipeline {0} already has active WA Channel Context {1}.").format(self.pipeline, duplicate))

        account = frappe.get_cached_doc("Chat Channel Account", self.channel_account)
        if not account.is_active:
            frappe.throw(_("Channel Account {0} is not active.").format(self.channel_account))
        if account.channel_type != "Interakt":
            frappe.throw(_("WA Channel Context currently supports Interakt accounts only."))
