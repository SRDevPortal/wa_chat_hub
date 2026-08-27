from frappe.model.document import Document
from frappe.utils import cint

class WAChatHubSettings(Document):
    def validate(self):
        if not cint(self.enable_autopilot_reply_batching):
            self.text_autopilot_reply_delay_seconds = 0
            self.media_autopilot_reply_delay_seconds = 0
