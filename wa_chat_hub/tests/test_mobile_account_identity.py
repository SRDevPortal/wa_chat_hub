from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from wa_chat_hub import mobile_account_identity as identity
from wa_chat_hub.api import mobile_app as api
from wa_chat_hub import services


class TestMobileAccountIdentity(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(frappe.conf, {
            "mobile_app_ai_account_identity": 1,
            "mobile_app_ai_require_country_code": 1,
        }))
        self.stack.enter_context(patch.object(api, "_require_backend_token"))
        self.stack.enter_context(patch.object(frappe.db, "commit"))
        self.stack.enter_context(patch.object(services, "task_log"))
        self.stack.enter_context(patch.object(services, "webhook_idempotency_enabled", return_value=True))
        self.stack.enter_context(patch("wa_chat_hub.messaging.windows.update_windows_on_message"))
        self.stack.enter_context(patch.object(services, "_run_append_message_followups"))
        # Actual contacts run validation/naming. Messages/conversations use the
        # real database without automation hooks or external jobs.
        original_insert = services.safe_ai_insert
        def insert(doc, **kwargs):
            if doc.doctype == "Chat Contact":
                return original_insert(doc, **kwargs)
            doc.db_insert()
            return doc
        self.stack.enter_context(patch.object(services, "safe_ai_insert", side_effect=insert))
        self.savepoint = "mobile_account_" + uuid4().hex
        frappe.db.savepoint(self.savepoint)
        self.addCleanup(lambda: frappe.db.rollback(save_point=self.savepoint))
        self.account = "_Test account " + uuid4().hex
        frappe.get_doc({"doctype": "Chat Channel Account", "name": self.account,
            "channel_type": "Mobile App", "is_active": 1}).db_insert()
        self.addCleanup(lambda: frappe.clear_document_cache("Chat Channel Account", self.account))
        self.stack.enter_context(patch.object(api, "_mobile_channel_account", return_value=self.account))
        self.user = self.make_user()

    def make_user(self, phone=None):
        user = frappe.get_doc({"doctype": "Mobile App User", "name": uuid4().hex,
            "external_id": uuid4().hex, "full_name": "Test email user",
            "email": uuid4().hex + "@example.test", "is_active": 1, "phone": phone})
        user.db_insert()
        return user

    def context(self, user=None, profile=None):
        return api._resolve_context((user or self.user).name, profile)

    def test_no_phone_no_patient_opens_and_reopens(self):
        first = api.open_session(self.user.name)["data"]
        second = api.open_session(self.user.name)["data"]
        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertIsNone(first["patient"])
        self.assertFalse(first["profile_required"])
        convo = frappe.get_doc("Chat Conversation", first["conversation_id"])
        self.assertFalse(frappe.db.get_value("Chat Contact", convo.contact, "phone_number"))

    def test_identical_local_numbers_do_not_share_chat(self):
        first = self.make_user("9990507831")
        second = self.make_user("9990507831")
        a = api._ensure_conversation(self.context(first))
        b = api._ensure_conversation(self.context(second))
        self.assertNotEqual(a, b)
        with self.assertRaises(frappe.PermissionError):
            api._assert_conversation_owner(a, self.context(second))

    def test_phone_edit_does_not_change_chat(self):
        before = api._ensure_conversation(self.context())
        frappe.db.set_value("Mobile App User", self.user.name, "phone", "12345")
        self.assertEqual(before, api._ensure_conversation(self.context()))

    def test_profiles_are_isolated_and_other_profile_is_rejected(self):
        self.user.set("profiles", [dict(name="profile-A"), dict(name="profile-B")])
        with patch.object(api, "_mobile_user", return_value=self.user):
            a = self.context(profile="profile-A")
            b = self.context(profile="profile-B")
            conversation = api._ensure_conversation(a)
            self.assertNotEqual(conversation, api._ensure_conversation(b))
            with self.assertRaises(frappe.PermissionError):
                api._assert_conversation_owner(conversation, b)
            with self.assertRaises(frappe.PermissionError):
                self.context(profile="other-users-profile")
            with self.assertRaises(frappe.ValidationError):
                self.context()

    def test_single_profile_is_resolved_without_patient_or_phone(self):
        self.user.set("profiles", [dict(name="profile-A", patient_id="Unverified Patient")])
        with patch.object(api, "_mobile_user", return_value=self.user), patch.object(api, "_patient_from_phone") as lookup:
            context = self.context()
            self.assertEqual(context["profile_key"], "profile-A")
            self.assertIsNone(context["patient"])
            lookup.assert_not_called()

    def test_legacy_phone_history_is_not_claimed(self):
        context = self.context()
        contact = "legacy-" + uuid4().hex
        frappe.get_doc({"doctype": "Chat Contact", "name": contact,
            "phone_number": "9" + str(int(uuid4().hex, 16) % 10**9).zfill(9)}).db_insert()
        old = frappe.get_doc({"doctype": "Chat Conversation", "channel_account": self.account,
            "contact": contact})
        old.db_insert()
        with self.assertRaises(frappe.PermissionError):
            api._assert_conversation_owner(old.name, context)

    def test_text_attachments_and_outbound_replies_stay_on_account_chat(self):
        convo = api._ensure_conversation(self.context())
        for direction, sender, kind in [("Inbound", "Customer", "Text"),
                ("Inbound", "Customer", "Image"), ("Inbound", "Customer", "Audio"),
                ("Inbound", "Customer", "Document"), ("Outbound", "AI", "Text"),
                ("Outbound", "Agent", "Text")]:
            result = services._append_message_impl({
                "channel_account": self.account, "conversation": convo,
                "provider_name": "Mobile App", "provider_message_id": uuid4().hex,
                "direction": direction, "sender_type": sender, "content_type": kind,
                "body": "Test", "media_url": "https://example.test/file",
            })
            self.assertEqual(str(result["conversation"]), str(convo))
        self.assertEqual(frappe.db.count("Chat Message", {"conversation": convo}), 6)

    def test_duplicate_message_is_not_inserted_twice(self):
        convo = api._ensure_conversation(self.context())
        payload = dict(channel_account=self.account, conversation=convo,
            provider_name="Mobile App", provider_message_id=uuid4().hex,
            direction="Inbound", sender_type="Customer", content_type="Text", body="Test")
        a = services._append_message_impl(payload)
        b = services._append_message_impl(payload)
        self.assertEqual(a["message"], b["message"])
        self.assertTrue(b["duplicate"])

    def test_other_channels_never_enable_account_mode(self):
        with patch.object(frappe, "get_cached_doc", return_value=frappe._dict(channel_type="Interakt")):
            self.assertFalse(identity.enabled(self.account))
        with patch.dict(frappe.conf, {"mobile_app_ai_account_identity": 0}):
            self.assertFalse(identity.enabled(self.account))

    def test_wrong_channel_is_denied(self):
        context = self.context()
        convo = api._ensure_conversation(context)
        context["account"] = "Other Mobile App channel"
        with self.assertRaises(frappe.PermissionError):
            api._assert_conversation_owner(convo, context)

    def test_guest_backend_request_can_open_after_shared_token_validation(self):
        previous = frappe.session.user
        try:
            frappe.set_user("Guest")
            result = api.open_session(self.user.name)
            self.assertTrue(result["success"])
        finally:
            frappe.set_user(previous)

    def test_non_mobile_contact_still_requires_phone(self):
        doc = frappe.get_doc({"doctype": "Chat Contact", "display_name": "Missing phone"})
        with self.assertRaises(frappe.ValidationError):
            doc.insert(ignore_permissions=True)

    def test_message_api_and_attachment_api_need_no_phone(self):
        convo = api._ensure_conversation(self.context())
        with patch.object(api, "append_message", side_effect=services._append_message_impl):
            text = api.send_message(self.user.name, str(convo), "Hello", uuid4().hex)
            attachment = api.send_attachment(self.user.name, str(convo), "Image",
                "https://example.test/file.png", "file.png", uuid4().hex)
        self.assertTrue(text["success"])
        self.assertTrue(attachment["success"])
        self.assertEqual(len(api.get_messages(self.user.name, str(convo))["data"]["messages"]), 2)

    def test_all_read_write_endpoints_reject_another_account(self):
        convo = str(api._ensure_conversation(self.context()))
        other = self.make_user()
        calls = [
            lambda: api.get_messages(other.name, convo),
            lambda: api.send_message(other.name, convo, "Hello", uuid4().hex),
            lambda: api.send_attachment(other.name, convo, "Image", "https://example.test/a", "a", uuid4().hex),
            lambda: api.escalate(other.name, convo),
        ]
        for call in calls:
            with self.assertRaises(frappe.PermissionError):
                call()

    def test_care_team_confirmation_targets_same_conversation(self):
        convo = str(api._ensure_conversation(self.context()))
        # Ticket insertion uses the real schema; skip its notification hooks.
        from frappe.model.document import Document
        original = Document.insert
        def insert(doc, *args, **kwargs):
            if doc.doctype == "Support Ticket":
                doc.name = uuid4().hex
                doc.db_insert()
                return doc
            return original(doc, *args, **kwargs)
        with patch.object(Document, "insert", insert), patch.object(api, "append_message", side_effect=services._append_message_impl):
            result = api.escalate(self.user.name, convo)
            repeat = api.escalate(self.user.name, convo)
        self.assertTrue(result["data"]["created"])
        self.assertFalse(repeat["data"]["created"])
        self.assertEqual(frappe.db.count("Chat Message", {"conversation": convo}), 1)

    def test_ai_reply_passes_explicit_conversation_without_phone(self):
        from wa_chat_hub.api import ai_bot
        convo = str(api._ensure_conversation(self.context()))
        with patch.object(ai_bot, "send_outbound_message", return_value={"delivery_status": "Sent"}), patch.object(ai_bot, "append_message", side_effect=services._append_message_impl):
            ai_bot._deliver_ai_reply(convo, "Hello from the assistant")
        self.assertEqual(frappe.db.count("Chat Message", {"conversation": convo, "sender_type": "AI"}), 1)

    def test_phone_contact_naming_is_preserved(self):
        phone = "9" + str(int(uuid4().hex, 16) % 10**9).zfill(9)
        doc = frappe.get_doc({"doctype": "Chat Contact", "phone_number": phone})
        doc.insert(ignore_permissions=True)
        self.assertEqual(doc.name, phone)
        self.assertEqual(doc.phone_number, phone)

    def test_disabled_mode_cannot_access_account_chat_via_phone(self):
        context = self.context()
        convo = api._ensure_conversation(context)
        legacy = dict(context, phone="+919990507831")
        legacy.pop("mobile_account_key")
        with self.assertRaises(frappe.PermissionError):
            api._assert_conversation_owner(convo, legacy)
