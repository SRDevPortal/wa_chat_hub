import frappe
from frappe.model.document import Document


class ChatContact(Document):
    def autoname(self):
        self.name = "mobile-" + self.mobile_account_key if self.mobile_account_key else self.phone_number

    def validate(self):
        if self.mobile_account_key:
            from wa_chat_hub.mobile_account_identity import identity_key

            account = frappe.get_cached_doc("Chat Channel Account", self.mobile_channel_account)
            if account.channel_type != "Mobile App" or not self.mobile_app_user:
                frappe.throw("Account chat contacts require a Mobile App channel and user.")
            expected = identity_key(self.mobile_channel_account, self.mobile_app_user, self.mobile_profile_id or "")
            if self.mobile_account_key != expected or self.phone_number:
                frappe.throw("Invalid Mobile App contact identity.")
        elif not self.phone_number:
            frappe.throw("A phone number is required for this contact.")
