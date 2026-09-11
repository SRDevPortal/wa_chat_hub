from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from wa_chat_hub.channel_resolver import _get_or_create_reference_conversation
from wa_chat_hub.connector.interakt.adapter import extract_interakt_customer_phone
from wa_chat_hub.phone_normalization import chat_phone_candidates, normalize_chat_phone
from wa_chat_hub.services import (
    _append_message_impl,
    _append_message_lock_name,
    find_conversation_for_phone,
    get_or_create_contact,
    resolve_conversation,
)


class TestChatPhoneNormalization(TestCase):
    def test_indian_number_formats_have_one_identity_and_lock(self):
        variants = ["9876543210", "+91 98765 43210", "919876543210", "00919876543210", "09876543210"]
        for phone in variants:
            with self.subTest(phone=phone):
                self.assertEqual(normalize_chat_phone(phone), "919876543210")
                self.assertEqual(
                    _append_message_lock_name({"channel_account": "A", "phone_number": phone}),
                    _append_message_lock_name({"channel_account": "A", "phone_number": variants[0]}),
                )

    def test_international_numbers_do_not_become_indian_aliases(self):
        for phone, expected in [("+1 415 555 0123", "14155550123"), ("0044 7700 900123", "447700900123")]:
            self.assertEqual(normalize_chat_phone(phone), expected)
            self.assertEqual(chat_phone_candidates(phone), [expected])
        self.assertEqual(chat_phone_candidates(""), [])

    def test_webhook_country_code_is_used_with_local_number(self):
        self.assertEqual(extract_interakt_customer_phone({"country_code": "+91", "phone_number": "9876543210"}), "+919876543210")
        self.assertEqual(extract_interakt_customer_phone({"countryCode": "+1", "phoneNumber": "4155550123"}), "+14155550123")
        self.assertEqual(extract_interakt_customer_phone({"country_code": "+91", "phone_number": "919876543210"}), "919876543210")


class TestChatThreadContinuity(TestCase):
    """Exercise real database lookup/storage with provider calls and hooks isolated."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("wa_chat_hub.services.safe_ai_insert", side_effect=self.insert_without_hooks))
        self.stack.enter_context(patch("wa_chat_hub.services.task_log"))
        self.stack.enter_context(patch("wa_chat_hub.services.route_conversation", return_value={}))
        self.stack.enter_context(patch("wa_chat_hub.services.webhook_idempotency_enabled", return_value=True))
        self.stack.enter_context(patch("wa_chat_hub.messaging.windows.update_windows_on_message"))
        # No test can commit fixtures or trigger application follow-up jobs.
        self.stack.enter_context(patch.object(frappe.db, "commit"))
        self.stack.enter_context(patch("wa_chat_hub.services._run_append_message_followups"))
        self.savepoint = "continuity_" + uuid4().hex
        frappe.db.savepoint(self.savepoint)
        self.addCleanup(lambda: frappe.db.rollback(save_point=self.savepoint))
        self.phone = "9" + str(int(uuid4().hex, 16) % 10**9).zfill(9)
        self.account = "_Test continuity " + uuid4().hex
        frappe.get_doc({"doctype": "Chat Channel Account", "name": self.account, "channel_type": "Interakt", "is_active": 1}).db_insert()

    @staticmethod
    def insert_without_hooks(doc, **kwargs):
        doc.db_insert()
        return doc

    def legacy_contact(self):
        frappe.get_doc({"doctype": "Chat Contact", "name": self.phone, "phone_number": self.phone}).db_insert()
        return self.phone

    def open_reference(self, contact, reference_doctype="Patient"):
        return _get_or_create_reference_conversation(
            contact=contact, channel_account=self.account,
            reference_doctype=reference_doctype, reference_name="_Test patient or lead",
            defer_reference_link=reference_doctype == "CRM Lead",
        )[0]

    def append(self, phone, direction, conversation=None):
        return _append_message_impl({
            "channel_account": self.account, "phone_number": phone,
            "conversation": conversation, "direction": direction,
            "sender_type": "Agent" if direction == "Outbound" else "Customer",
            "content_type": "Template" if direction == "Outbound" else "Text",
            "body": "Test template" if direction == "Outbound" else "Patient reply",
            "provider_name": "Interakt", "channel_message_id": uuid4().hex,
        })

    def test_patient_template_and_reply_share_conversation(self):
        contact = get_or_create_contact(self.phone)
        self.assertEqual(frappe.db.get_value("Chat Contact", contact, "phone_number"), "91" + self.phone)
        conversation = self.open_reference(contact)
        template = self.append(self.phone, "Outbound", conversation)
        reply = self.append("+91" + self.phone, "Inbound")
        self.assertEqual(template["conversation"], conversation)
        self.assertEqual(reply["conversation"], conversation)
        self.assertEqual(frappe.db.count("Chat Conversation", {"channel_account": self.account}), 1)
        self.assertEqual(frappe.db.count("Chat Message", {"conversation": conversation}), 2)

    def test_legacy_local_contact_and_closed_chat_are_reused(self):
        contact = self.legacy_contact()
        conversation = self.open_reference(contact)
        frappe.db.set_value("Chat Conversation", conversation, "status", "Closed")
        self.assertEqual(get_or_create_contact("+91" + self.phone), contact)
        reply = self.append("91" + self.phone, "Inbound")
        self.assertEqual(reply["conversation"], conversation)
        self.assertEqual(frappe.db.get_value("Chat Conversation", conversation, "status"), "Open")

    def test_closed_crm_lead_template_and_reply_share_conversation(self):
        conversation = self.open_reference(get_or_create_contact(self.phone), "CRM Lead")
        frappe.db.set_value("Chat Conversation", conversation, "status", "Closed")
        self.assertEqual(self.append(self.phone, "Outbound", conversation)["conversation"], conversation)
        self.assertEqual(self.append("91" + self.phone, "Inbound")["conversation"], conversation)

    def test_historical_alias_contacts_choose_thread_with_messages(self):
        legacy = self.legacy_contact()
        old = self.open_reference(legacy)
        canonical = "91" + self.phone
        frappe.get_doc({"doctype": "Chat Contact", "name": canonical, "phone_number": canonical}).db_insert()
        populated = frappe.get_doc({"doctype": "Chat Conversation", "channel_account": self.account, "contact": canonical, "status": "Open"})
        populated.db_insert()
        self.append(canonical, "Outbound", populated.name)
        self.assertNotEqual(old, populated.name)
        self.assertEqual(find_conversation_for_phone(self.phone, self.account), populated.name)
        self.assertEqual(self.append(self.phone, "Inbound")["conversation"], populated.name)

    def test_different_accounts_stay_separate_and_cannot_be_pinned(self):
        contact = get_or_create_contact(self.phone)
        original = self.open_reference(contact)
        other_account = self.account + " other"
        other, created = resolve_conversation(other_account, contact)
        self.assertTrue(created)
        self.assertNotEqual(original, other)
        self.assertEqual(find_conversation_for_phone(self.phone, self.account), original)
        with self.assertRaises(frappe.ValidationError):
            resolve_conversation(other_account, contact, preferred_conversation=original)

    def test_vobiz_template_api_records_in_selected_chat(self):
        from vobiz_click_to_call.api.console import send_whatsapp_template

        conversation = self.open_reference(self.legacy_contact())
        frappe.db.set_value("Chat Conversation", conversation, "status", "Closed")
        with (
            patch("vobiz_click_to_call.api.console._ensure_whatsapp_conversation_read"),
            patch("vobiz_click_to_call.api.console._whatsapp_messages_page", return_value={}),
            patch("wa_chat_hub.outbound.send_interakt_template_message", return_value={"provider_message_id": uuid4().hex, "delivery_status": "Sent"}) as send,
        ):
            result = send_whatsapp_template(conversation, "test_template")
        self.assertEqual(send.call_args.args[0], conversation)
        self.assertEqual(result["result"]["conversation"], conversation)
        self.assertEqual(self.append("+91" + self.phone, "Inbound")["conversation"], conversation)

    def test_creation_retry_rechecks_for_conversation(self):
        contact = get_or_create_contact(self.phone)
        conversation = self.open_reference(contact)
        with (
            patch("wa_chat_hub.services.find_conversation_for_phone", side_effect=[frappe.QueryDeadlockError(), conversation]) as lookup,
            patch("wa_chat_hub.db_retry.time.sleep"),
            patch("wa_chat_hub.db_retry.task_log"),
        ):
            self.assertEqual(resolve_conversation(self.account, contact), (conversation, False))
        self.assertEqual(lookup.call_count, 2)

    def test_missing_account_cannot_reuse_another_accounts_chat(self):
        contact = get_or_create_contact(self.phone)
        self.open_reference(contact)
        with self.assertRaises(frappe.ValidationError):
            resolve_conversation("", contact)
