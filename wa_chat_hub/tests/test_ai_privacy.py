import unittest
from unittest.mock import patch

import frappe
from wa_chat_hub.api import ai
from wa_chat_hub import number_privacy


class AIResponsePrivacyTests(unittest.TestCase):
    def test_restricted_responses_mask_text_without_changing_suggestions(self):
        for endpoint, generator in ((ai.summarize_conversation, "generate_summary"),
                                    (ai.draft_reply, "generate_reply_draft")):
            suggestion = {"name": "suggestion-1", "content": "Call 9876501234"}
            with patch.object(ai, "ensure_can_read_conversation") as authorize, \
                 patch.object(ai, generator, return_value=suggestion) as generate, \
                 patch.object(number_privacy, "restricted", return_value=True):
                result = endpoint("1042")
            authorize.assert_called_once_with("1042")
            generate.assert_called_once_with("1042")
            self.assertNotIn("9876501234", result["result"]["content"])
            self.assertEqual(result["result"]["name"], "suggestion-1")
            self.assertTrue(result["success"])
            self.assertEqual(suggestion["content"], "Call 9876501234")

    def test_full_visibility_keeps_original_response(self):
        suggestion = {"name": "suggestion-1", "content": "Call 9876501234"}
        with patch.object(ai, "ensure_can_read_conversation"), \
             patch.object(ai, "generate_summary", return_value=suggestion), \
             patch.object(number_privacy, "restricted", return_value=False):
            self.assertIs(ai.summarize_conversation("1042")["result"], suggestion)

    def test_denied_conversation_never_generates_suggestions(self):
        for endpoint, generator in ((ai.summarize_conversation, "generate_summary"),
                                    (ai.draft_reply, "generate_reply_draft")):
            with patch.object(ai, "ensure_can_read_conversation", side_effect=frappe.PermissionError), \
                 patch.object(ai, generator) as generate:
                with self.assertRaises(frappe.PermissionError):
                    endpoint("1042")
                generate.assert_not_called()
