from frappe.model.document import Document


class ChatChannelAccount(Document):
    def validate(self):
        from wa_chat_hub.interakt.template_selection import validate_account
        validate_account(self)
        if self.llm_provider:
            import frappe
            from wa_chat_hub.ai.providers import OPENAI_COMPATIBLE_PROVIDER_TYPES
            provider = frappe.get_doc("WA LLM Provider", self.llm_provider)
            if not provider.is_active or not provider.use_for_chat or provider.provider_type not in OPENAI_COMPATIBLE_PROVIDER_TYPES:
                frappe.throw("Select an active, OpenAI-compatible LLM Provider with Use for Chat Replies enabled.")

    def on_update(self):
        from wa_chat_hub.interakt.template_selection import clear_cache
        clear_cache(self)
