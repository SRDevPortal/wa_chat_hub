import frappe
from frappe import _
from frappe.model.document import Document


class WAAIDepartmentProfile(Document):
    def validate(self):
        if not self.is_active:
            return
        duplicate = frappe.db.get_value(
            "WA AI Department Profile",
            {
                "name": ["!=", self.name],
                "agent_profile": self.agent_profile,
                "medical_department": self.medical_department,
                "is_active": 1,
            },
            "name",
        )
        if duplicate:
            frappe.throw(
                _("Agent {0} already has active department profile {1} for {2}.").format(
                    self.agent_profile, duplicate, self.medical_department
                )
            )
