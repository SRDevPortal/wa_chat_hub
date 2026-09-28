from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from wa_chat_hub.api import mobile_app as api
from wa_chat_hub.phone_normalization import mobile_phone_candidates
from wa_chat_hub import services


class TestMobilePhoneIdentity(TestCase):
    def test_domestic_formats_have_one_identity(self):
        for phone in ("9876543210", "+91 98765 43210", "919876543210", "00919876543210", "09876543210"):
            with self.subTest(phone=phone):
                self.assertEqual(mobile_phone_candidates(phone, "IN")[0], "+919876543210")

    def test_country_specific_local_numbers(self):
        for phone, region, expected in (
            ("4155550123", "US", "+14155550123"),
            ("020 7946 0018", "GB", "+442079460018"),
            ("+39 02 3661 8300", "US", "+390236618300"),
            ("+45 32 12 34 56", "IN", "+4532123456"),
            ("0044 7700 900123", "IN", "+447700900123"),
            ("14155550123", "IN", "+14155550123"),
        ):
            with self.subTest(phone=phone):
                self.assertEqual(mobile_phone_candidates(phone, region)[0], expected)

    def test_foreign_number_does_not_get_local_alias(self):
        aliases = mobile_phone_candidates("+1 9876543210", "IN")
        self.assertNotIn("9876543210", aliases)
        self.assertNotEqual(aliases[0], mobile_phone_candidates("9876543210", "IN")[0])

    def test_missing_country_or_invalid_number_fails_closed(self):
        for phone in ("", "123", "4155550123"):
            self.assertEqual(mobile_phone_candidates(phone), [])

    def test_short_international_number_does_not_claim_ambiguous_local_contact(self):
        self.assertNotIn("6581234567", mobile_phone_candidates("+65 8123 4567", "IN"))
        self.assertNotEqual(
            mobile_phone_candidates("+65 8123 4567", "IN")[0],
            mobile_phone_candidates("6581234567", "IN")[0],
        )


class TestMobileAppOwnership(TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(api, "_", side_effect=lambda value: value))
        if not hasattr(frappe.local, "flags"):
            frappe.local.flags = frappe._dict(in_test=False)
            self.addCleanup(lambda: delattr(frappe.local, "flags"))
        self.db = self.stack.enter_context(patch.object(api.frappe, "db", MagicMock(), create=True))
        self.stack.enter_context(patch.object(api.frappe, "conf", frappe._dict(), create=True))
        self.account = frappe._dict(name="APP", channel_type="Mobile App", interakt_default_country_code="+91")
        self.stack.enter_context(patch.object(api.frappe, "get_cached_doc", return_value=self.account))
        self.convo = frappe._dict(name="CHAT", channel_account="APP", contact="CONTACT", linked_patient="PATIENT")
        self.get_doc = self.stack.enter_context(patch.object(api.frappe, "get_doc", side_effect=self.read_doc))
        self.stack.enter_context(patch.object(api.frappe, "throw", side_effect=self.throw))
        self.context = dict(
            account="APP", phone="+919876543210", patient="PATIENT", profile_patient=None,
            user=frappe._dict(full_name="Test user"),
        )
        self.db.get_value.return_value = "9876543210"

    @staticmethod
    def throw(message, exc=frappe.ValidationError, **kwargs):
        raise exc(message)

    def read_doc(self, doctype, name):
        return self.convo if doctype == "Chat Conversation" else self.account

    def test_legacy_local_contact_can_open(self):
        self.assertIs(api._assert_conversation_owner("CHAT", self.context), self.convo)

    def test_authorized_family_profile_can_have_different_phone(self):
        self.context["profile_patient"] = "PATIENT"
        self.db.get_value.return_value = "+14155550123"
        self.assertIs(api._assert_conversation_owner("CHAT", self.context), self.convo)

    def test_phone_inferred_patient_cannot_bypass_phone_check(self):
        self.db.get_value.return_value = "+14155550123"
        with self.assertRaises(frappe.PermissionError):
            api._assert_conversation_owner("CHAT", self.context)

    def test_same_suffix_in_another_country_is_denied(self):
        self.db.get_value.return_value = "+1 9876543210"
        with self.assertRaises(frappe.PermissionError):
            api._assert_conversation_owner("CHAT", self.context)

    def test_different_patient_is_denied_even_when_phone_matches(self):
        self.convo.linked_patient = "OTHER"
        self.context["profile_patient"] = "PATIENT"
        with self.assertRaises(frappe.PermissionError):
            api._assert_conversation_owner("CHAT", self.context)

    def test_other_channel_is_denied_even_with_profile_link(self):
        self.context["profile_patient"] = "PATIENT"
        for field, value in (("name", "OTHER-APP"), ("channel_type", "Interakt")):
            with self.subTest(field=field), patch.dict(self.account, {field: value}):
                with self.assertRaises(frappe.PermissionError):
                    api._assert_conversation_owner("CHAT", self.context)

    def test_unlinked_conversation_requires_login_phone(self):
        self.convo.linked_patient = None
        self.context["profile_patient"] = "PATIENT"
        self.db.get_value.return_value = "+14155550123"
        with self.assertRaises(frappe.PermissionError):
            api._assert_conversation_owner("CHAT", self.context)

    def test_foreign_profile_identifier_is_rejected(self):
        user = frappe._dict(profiles=[frappe._dict(name="P1", patient_id="PATIENT")])
        with self.assertRaises(frappe.PermissionError):
            api._profile_patient(user, "OTHER")

    def test_multiple_profiles_require_selection(self):
        user = frappe._dict(profiles=[frappe._dict(patient_id=name) for name in ("P1", "P2")])
        self.db.exists.return_value = True
        with self.assertRaises(frappe.ValidationError):
            api._profile_patient(user)

    def test_index_suffix_match_requires_actual_phone_match(self):
        policy = MagicMock()
        policy.section.return_value = {"phone_fields": {"Patient": ["mobile"]}}
        self.get_doc.side_effect = None
        self.get_doc.return_value = frappe._dict(mobile="+1 9876543210")
        with patch.object(api, "get_channel_policy", return_value=policy), patch.object(
            api, "find_indexed_phone_match_names", return_value={"OTHER"}
        ):
            self.assertIsNone(api._patient_from_phone("+919876543210", "APP"))
            self.get_doc.return_value.mobile = "9876543210"
            self.assertEqual(api._patient_from_phone("+919876543210", "APP"), "OTHER")

    def test_configured_region_overrides_legacy_interakt_default(self):
        with patch.object(api.frappe, "conf", frappe._dict(mobile_app_ai_phone_region="US")):
            self.assertEqual(services.channel_phone_candidates("4155550123", "APP")[0], "+14155550123")

    def test_text_and_attachment_use_authorized_chat_phone_and_id(self):
        self.context["profile_patient"] = "PATIENT"
        self.db.get_value.return_value = "+14155550123"
        with (
            patch.object(api, "_require_backend_token"),
            patch.object(api, "_resolve_context", return_value=self.context),
            patch.object(api, "append_message", return_value={"message": "MSG"}) as append,
        ):
            api.send_message("USER", "CHAT", "Hello", "ID-1")
            api.send_attachment("USER", "CHAT", "Image", "https://example.test/image.jpg", "Image", "ID-2")
        for call in append.call_args_list:
            self.assertEqual(call.args[0]["phone_number"], "+14155550123")
            self.assertEqual(call.args[0]["conversation"], "CHAT")

    def test_resolve_context_retains_source_of_patient_authorization(self):
        user = frappe._dict(phone="9876543210")
        with (
            patch.object(api, "_mobile_user", return_value=user),
            patch.object(api, "_mobile_channel_account", return_value="APP"),
            patch.object(api, "_profile_patient", return_value="PATIENT"),
            patch.object(api, "_patient_from_phone") as fallback,
        ):
            context = api._resolve_context("USER", "PROFILE")
        self.assertEqual(context["phone"], "+919876543210")
        self.assertEqual(context["profile_patient"], "PATIENT")
        fallback.assert_not_called()
