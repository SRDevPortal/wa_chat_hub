import unittest
from copy import deepcopy
from unittest.mock import MagicMock, patch

import frappe
from wa_chat_hub import number_privacy as privacy


class CustomerReplyPrivacyTests(unittest.TestCase):
    def test_only_inbound_customer_body_is_unmasked(self):
        for direction, sender, allowed in [
            ("Inbound", "Customer", True),
            ("Inbound", "System", False),
            ("Outbound", "Customer", False),
            ("Outbound", "Agent", False),
            ("Outbound", "AI", False),
            ("Internal Note", "Agent", False),
            ("Inbound", None, False),
        ]:
            with self.subTest(direction=direction, sender=sender):
                raw = {"direction": direction, "sender_type": sender,
                       "body": "Call 9876543210. My age is 27.",
                       "phone_number": "9876543210", "ai_summary": "Phone 9876543210",
                       "raw_transport_payload": {"number": "9876543210"}}
                original = deepcopy(raw)
                result = privacy.project({"result": [raw]})["result"][0]
                self.assertEqual("9876543210" in result["body"], allowed)
                self.assertIn("27", result["body"])
                self.assertNotIn("9876543210", result["phone_number"])
                self.assertNotIn("9876543210", result["ai_summary"])
                self.assertNotIn("raw_transport_payload", result)
                self.assertEqual(raw, original)

    def test_preview_requires_matching_verified_message(self):
        raw = {"name": "123", "last_message_preview": "Call 9876543210",
               "contact_phone_number": "9876543210"}
        self.assertNotIn("9876543210", privacy.project(raw)["last_message_preview"])
        result = privacy.project(raw, customer_previews={"123": raw["last_message_preview"]})
        self.assertEqual(result["last_message_preview"], raw["last_message_preview"])
        self.assertNotIn("9876543210", result["contact_phone_number"])
        for previews in [{"456": raw["last_message_preview"]}, {"123": "Different 9876543210"}]:
            self.assertNotIn("9876543210", privacy.project(raw, customer_previews=previews)["last_message_preview"])

    def test_one_bounded_query_classifies_only_latest_customer_previews(self):
        rows = [frappe._dict(conversation="123", direction="Inbound", sender_type="Customer", body="Call 9876543210", content_type="Text", media_url=None),
                frappe._dict(conversation="456", direction="Outbound", sender_type="Agent", body="Call 9123456780", content_type="Text", media_url=None)]
        with patch.object(privacy.frappe, "db", MagicMock()) as db:
            db.sql.return_value = rows
            result = privacy.customer_preview_values({"result": [{"name": "123", "last_message_preview": "Call 9876543210"}, {"name": "456", "last_message_preview": "Call 9123456780"}]})
            self.assertEqual(result, {"123": "Call 9876543210"})
            db.sql.assert_called_once()
            self.assertIn("limit 1", db.sql.call_args.args[0])
            self.assertEqual(db.sql.call_args.args[1]["names"], ("123", "456"))

    def test_non_preview_payload_never_queries_messages(self):
        with patch.object(privacy.frappe, "db", MagicMock()) as db:
            self.assertEqual(privacy.customer_preview_values({"body": "Text"}), {})
            self.assertEqual(privacy.customer_preview_values([{"name": str(i), "last_message_preview": "Text"} for i in range(201)]), {})
            db.sql.assert_not_called()

    def test_live_notice_still_contains_no_customer_body(self):
        message = MagicMock()
        message.name = "123"
        message.direction = "Inbound"
        message.sender_type = "Customer"
        with patch.object(privacy, "enabled", return_value=True):
            result = privacy.message_event("456", message)
        self.assertNotIn("body", result)
        self.assertNotIn("message", result)
        self.assertTrue(result["refresh_required"])

    def test_get_messages_authorized_response_shows_customer_body(self):
        from wa_chat_hub.api import chat
        raw = frappe._dict(name="123", direction="Inbound", sender_type="Customer", body="Call 9876543210", content_type="Text", raw_transport_payload={"phone": "9876543210"})
        with patch.object(privacy, "restricted", return_value=True), patch.object(chat, "ensure_can_read_conversation") as auth, patch.object(chat.frappe, "get_all", return_value=[raw]), patch.object(chat, "_attach_message_file_urls"):
            result = chat.get_messages.__wrapped__("456")
            auth.assert_called_once_with("456")
            self.assertEqual(result["result"][0]["body"], raw.body)
            self.assertNotIn("raw_transport_payload", result["result"][0])

    def test_cached_preview_is_verified_again_each_request(self):
        def response():
            return {"result": [{"name": "123", "last_message_preview": "Call 9876543210"}]}
        response.__module__ = "wa_chat_hub.api.chat"
        response.__name__ = "get_conversations"
        wrapped = privacy.browser_response(response)
        with patch.object(privacy, "restricted", return_value=True), patch.object(privacy, "customer_preview_values", side_effect=[{"123": "Call 9876543210"}, {}]) as verify:
            self.assertIn("9876543210", wrapped()["result"][0]["last_message_preview"])
            self.assertNotIn("9876543210", wrapped()["result"][0]["last_message_preview"])
            self.assertEqual(verify.call_count, 2)

    def test_full_visibility_does_not_change_or_classify_response(self):
        raw = {"body": "Call 9876543210", "phone_number": "9876543210"}
        wrapped = privacy.browser_response(lambda: raw)
        with patch.object(privacy, "restricted", return_value=False), patch.object(privacy, "customer_preview_values") as verify:
            self.assertIs(wrapped(), raw)
            verify.assert_not_called()
