import frappe
from frappe import _
from frappe.model.document import Document


class WAChannelPipelineMap(Document):
    def validate(self):
        pass


def _validate_active_link(doctype: str, name: str | None, message: str) -> None:
    if not name or not frappe.db.exists("DocType", doctype):
        return

    try:
        has_is_active = frappe.db.has_column(doctype, "is_active")
    except frappe.db.TableMissingError:
        return

    if not has_is_active:
        return

    is_active = frappe.db.get_value(doctype, name, "is_active")
    if not is_active:
        frappe.throw(message)


@frappe.whitelist()
def sync_contacts_to_interakt(pipeline_map: str):
    from wa_chat_hub.api.contact_sync import sync_pipeline_map_to_interakt

    return sync_pipeline_map_to_interakt(pipeline_map)
