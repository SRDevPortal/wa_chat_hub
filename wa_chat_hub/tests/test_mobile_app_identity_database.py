from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from wa_chat_hub.api import mobile_app as api
from wa_chat_hub.channel_resolver import _get_or_create_reference_conversation
from wa_chat_hub.services import _append_message_impl, get_or_create_contact, resolve_conversation


class TestMobileAppIdentityDatabase(TestCase):
    """Real storage and lookup, with rollback and no provider calls or jobs."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("wa_chat_hub.services.safe_ai_insert", side_effect=self.insert_without_hooks))
        self.stack.enter_context(patch("wa_chat_hub.services.task_log"))
        self.stack.enter_context(patch("wa_chat_hub.services.route_conversation", return_value={}))
        self.stack.enter_context(patch("wa_chat_hub.services.webhook_idempotency_enabled", return_value=True))
        self.stack.enter_context(patch("wa_chat_hub.messaging.windows.update_windows_on_message"))
        self.stack.enter_context(patch("wa_chat_hub.services._run_append_message_followups"))
        self.stack.enter_context(patch.object(frappe.db, "commit"))
        self.stack.enter_context(patch.dict(frappe.conf, {"mobile_app_ai_phone_region": "IN"}))
        self.savepoint = "mobile_identity_" + uuid4().hex
        frappe.db.savepoint(self.savepoint)
        self.addCleanup(lambda: frappe.db.rollback(save_point=self.savepoint))
        self.phone = "9" + str(int(uuid4().hex, 16) % 10**9).zfill(9)
        self.account = "_Test mobile " + uuid4().hex
        frappe.get_doc({
            "doctype": "Chat Channel Account", "name": self.account,
            "channel_type": "Mobile App", "is_active": 1, "interakt_default_country_code": "+91",
        }).db_insert()
        self.addCleanup(lambda: frappe.clear_document_cache("Chat Channel Account", self.account))

    @staticmethod
    def insert_without_hooks(doc, **kwargs):
        doc.db_insert()
        return doc

    def patient_chat(self, patient, contact):
        return _get_or_create_reference_conversation(
            contact=contact, channel_account=self.account, reference_doctype="Patient",
            reference_name=patient, patient_scope=patient,
        )[0]

    def test_existing_local_contact_and_chat_are_reused_on_open(self):
        contact = "_Test contact " + uuid4().hex
        frappe.get_doc({"doctype": "Chat Contact", "name": contact, "phone_number": self.phone}).db_insert()
        conversation, _ = resolve_conversation(self.account, contact)
        context = dict(
            phone="+91" + self.phone, account=self.account, patient=None, profile_patient=None,
            user=frappe._dict(full_name="Test user"),
        )
        with patch.object(api, "_require_backend_token"), patch.object(api, "_resolve_context", return_value=context):
            result = api.open_session("USER")
        self.assertEqual(result["data"]["conversation_id"], str(conversation))
        self.assertEqual(frappe.db.get_value("Chat Conversation", conversation, "contact"), contact)
        self.assertEqual(frappe.db.get_value("Chat Contact", contact, "phone_number"), self.phone)

    def test_family_patients_sharing_phone_keep_separate_conversations(self):
        contact = get_or_create_contact(self.phone, channel_account=self.account)
        first = self.patient_chat("_Test patient A", contact)
        second = self.patient_chat("_Test patient B", contact)
        self.assertNotEqual(first, second)
        self.assertEqual(self.patient_chat("_Test patient A", contact), first)
        self.assertEqual(frappe.db.get_value("Chat Conversation", first, "linked_patient"), "_Test patient A")

    def test_pre_profile_history_is_reused_without_reassigning_other_patient(self):
        contact = get_or_create_contact(self.phone, channel_account=self.account)
        old, _ = resolve_conversation(self.account, contact)
        self.assertEqual(self.patient_chat("_Test patient A", contact), old)
        self.assertNotEqual(self.patient_chat("_Test patient B", contact), old)

    def test_text_and_media_stay_in_selected_patient_chat(self):
        contact = get_or_create_contact(self.phone, channel_account=self.account)
        first = self.patient_chat("_Test patient A", contact)
        second = self.patient_chat("_Test patient B", contact)
        frappe.db.set_value("Chat Conversation", second, "last_message_time", "2099-01-01 00:00:00")
        for content_type in ("Text", "Image", "Audio", "Document"):
            result = _append_message_impl({
                "phone_number": "+91" + self.phone, "channel_account": self.account,
                "conversation": first, "provider_name": "Mobile App", "provider_message_id": uuid4().hex,
                "direction": "Inbound", "sender_type": "Customer", "content_type": content_type,
                "body": "Test", "media_url": "https://example.test/attachment" if content_type != "Text" else None,
            })
            self.assertEqual(result["conversation"], first)
            self.assertEqual(frappe.db.get_value("Chat Message", result["message"], "conversation"), str(first))
        self.assertEqual(frappe.db.count("Chat Message", {"conversation": second}), 0)

    def test_international_local_contact_reused_only_in_its_country(self):
        phone = "4155550123"
        contact = "_Test US contact " + uuid4().hex
        frappe.get_doc({"doctype": "Chat Contact", "name": contact, "phone_number": phone}).db_insert()
        with patch.dict(frappe.conf, {"mobile_app_ai_phone_region": "US"}):
            self.assertEqual(get_or_create_contact("+14155550123", channel_account=self.account), contact)
            india = get_or_create_contact("+914155550123", channel_account=self.account)
            self.assertNotEqual(india, contact)
