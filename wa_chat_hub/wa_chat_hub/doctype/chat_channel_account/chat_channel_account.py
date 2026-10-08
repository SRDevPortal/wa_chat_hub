from frappe.model.document import Document


class ChatChannelAccount(Document):
    def validate(self):
        from wa_chat_hub.interakt.template_selection import validate_account
        validate_account(self)

    def on_update(self):
        from wa_chat_hub.interakt.template_selection import clear_cache
        clear_cache(self)
