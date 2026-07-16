from frappe.model.document import Document
from frappe.utils import now_datetime

from wa_chat_hub.messaging.idempotency import (
    build_message_dedupe_key,
    webhook_idempotency_enabled,
)


class ChatMessage(Document):
    def before_validate(self):
        if not self.received_at:
            self.received_at = now_datetime()
        if webhook_idempotency_enabled() and not self.dedupe_key:
            self.dedupe_key = build_message_dedupe_key(
                conversation=self.conversation,
                provider_name=self.provider_name,
                provider_message_id=self.provider_message_id,
                channel_message_id=self.channel_message_id,
                provider_event_id=self.provider_event_id,
            )
