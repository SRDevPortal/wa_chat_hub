import frappe
from frappe import _
from frappe.model.document import Document


class WAChannelPipelineMap(Document):
    pass


@frappe.whitelist()
def sync_contacts_to_interakt(pipeline_map: str):
    from wa_chat_hub.api.contact_sync import sync_pipeline_map_to_interakt

    return sync_pipeline_map_to_interakt(pipeline_map)
