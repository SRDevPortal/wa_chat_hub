import frappe
from frappe import _
from frappe.model.document import Document


class WAAIAgentProfile(Document):
    def validate(self):
        if self.is_default and self.is_active:
            duplicate = frappe.db.get_value(
                "WA AI Agent Profile",
                {
                    "name": ["!=", self.name],
                    "agent_type": self.agent_type,
                    "is_default": 1,
                    "is_active": 1,
                },
                "name",
            )
            if duplicate:
                frappe.throw(
                    _("Only one active default {0} agent is allowed ({1}).").format(
                        self.agent_type, duplicate
                    )
                )

        if not 0 <= int(self.max_tool_calls or 0) <= 5:
            frappe.throw(_("Maximum Tool Calls must be between 0 and 5."))
