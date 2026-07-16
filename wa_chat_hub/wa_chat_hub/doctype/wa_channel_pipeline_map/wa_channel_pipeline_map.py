import frappe
from frappe import _
from frappe.model.document import Document


class WAChannelPipelineMap(Document):
    def validate(self):
        _validate_active_link(
            "Chat Channel Account",
            self.chat_channel_account,
            _("Select an active Chat Channel Account."),
        )
        _validate_active_link(
            "SR Lead Pipeline",
            self.sr_lead_pipeline,
            _("Select an active SR Lead Pipeline."),
        )
        if self.sr_lead_source:
            _validate_active_link(
                "SR Lead Source",
                self.sr_lead_source,
                _("Select an active SR Lead Source."),
            )
        if self.is_active and self.get("is_default"):
            self._validate_single_default_route()

    def _validate_single_default_route(self):
        existing = frappe.db.get_value(
            "WA Channel Pipeline Map",
            {
                "name": ["!=", self.name],
                "is_active": 1,
                "is_default": 1,
            },
            "name",
        )
        if existing:
            frappe.throw(_("Only one active default WA Channel Pipeline Map is allowed ({0}).").format(existing))


def _validate_active_link(doctype: str, name: str | None, message: str) -> None:
    if not name or not frappe.db.has_column(doctype, "is_active"):
        return
    is_active = frappe.db.get_value(doctype, name, "is_active")
    if not is_active:
        frappe.throw(message)


@frappe.whitelist()
def sync_contacts_to_interakt(pipeline_map: str):
    from wa_chat_hub.api.contact_sync import sync_pipeline_map_to_interakt

    return sync_pipeline_map_to_interakt(pipeline_map)
