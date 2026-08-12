from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.ai.ocr_summary import apply_prompt_priority_to_media_context
from wa_chat_hub.api.ai_bot import _build_latest_user_turn


class TestOCRPromptPriority(TestCase):
    @patch("wa_chat_hub.ai.ocr_summary.policy_section")
    def test_active_report_rule_suppresses_fallback(self, policy_section):
        policy_section.return_value = {"report_summary_prompt": "Share every extracted value."}

        result = apply_prompt_priority_to_media_context(
            "Attachment OCR / visual classification (reference data):\nHb 10",
            "For medical reports, share only the test name and never disclose values.",
            "ACCOUNT-1",
        )

        self.assertIn("active Agent Profile/System Prompt has highest priority", result)
        self.assertNotIn("Share every extracted value", result)
        self.assertIn("Hb 10", result)

    @patch("wa_chat_hub.ai.ocr_summary.policy_section")
    def test_fallback_applies_without_active_report_rule(self, policy_section):
        policy_section.return_value = {"report_summary_prompt": "Give a short neutral summary."}

        result = apply_prompt_priority_to_media_context(
            "Attachment OCR:\nHb 10",
            "Be polite and reply in the customer's language.",
            "ACCOUNT-1",
        )

        self.assertIn("Fallback report guidance", result)
        self.assertIn("Give a short neutral summary", result)

    def test_caption_and_ocr_remain_in_user_turn(self):
        message = frappe._dict(body="Please review this", content_type="Document")

        result = _build_latest_user_turn(message, "Attachment OCR:\nHb 10", False)

        self.assertIn("Please review this", result)
        self.assertIn("Attachment OCR", result)
