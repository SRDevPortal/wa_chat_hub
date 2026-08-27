from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from wa_chat_hub.ai.ocr_summary import (
    _append_to_lead_notes,
    _persist_ocr_result,
    apply_prompt_priority_to_media_context,
)
from wa_chat_hub.api.ai_bot import _build_latest_user_turn, _should_use_direct_vision


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

    def test_image_uses_direct_vision_even_when_ocr_is_separate(self):
        self.assertTrue(_should_use_direct_vision("https://example.test/report.jpg", "Image"))
        self.assertFalse(_should_use_direct_vision("", "Image"))
        self.assertFalse(_should_use_direct_vision("https://example.test/report.pdf", "Document"))

    @patch("wa_chat_hub.ai.ocr_summary.safe_ai_insert")
    @patch("wa_chat_hub.ai.ocr_summary.frappe.get_doc")
    @patch("wa_chat_hub.ai.ocr_summary.frappe.get_meta")
    @patch("wa_chat_hub.ai.ocr_summary.safe_ai_get_value")
    def test_ocr_result_is_persisted_independently(self, get_value, get_meta, get_doc, insert):
        get_value.side_effect = ["FILE-1", None, "PIPELINE-1"]
        get_meta.return_value.has_field.return_value = True
        result_doc = MagicMock(name="ocr_result")
        result_doc.name = "OCR-1"
        get_doc.return_value = result_doc
        convo = frappe._dict(
            name="CONV-1",
            linked_crm_lead="LEAD-1",
            channel_account="ACCOUNT-1",
        )

        result = _persist_ocr_result(
            convo=convo,
            crm_lead="LEAD-1",
            message_name="MSG-1",
            extracted="Image type: medical report\nReadable text: Hb 10.9",
            summary="Report summary: low haemoglobin",
        )

        self.assertEqual(result, "OCR-1")
        insert.assert_called_once_with(result_doc)
        payload = get_doc.call_args.args[0]
        self.assertEqual(payload["file"], "FILE-1")
        self.assertEqual(payload["status"], "Applied")
        self.assertIn("Hb 10.9", payload["raw_text"])

    @patch("wa_chat_hub.ai.ocr_summary.assert_ai_doctype_permission")
    @patch("wa_chat_hub.ai.ocr_summary._resolve_notes_fieldname", return_value="sr_lead_notes")
    @patch("wa_chat_hub.ai.ocr_summary.safe_ai_save")
    @patch("wa_chat_hub.ai.ocr_summary.safe_ai_get_doc")
    def test_lead_update_reloads_and_retries_timestamp_conflict(
        self, get_doc, save, _resolve_notes, _assert_permission
    ):
        stale = MagicMock(sr_lead_notes="old")
        fresh = MagicMock(sr_lead_notes="newer")
        get_doc.side_effect = [stale, fresh]
        save.side_effect = [frappe.TimestampMismatchError, None]

        _append_to_lead_notes("CRM Lead", "LEAD-1", "OCR block")

        self.assertEqual(get_doc.call_count, 2)
        self.assertEqual(save.call_count, 2)
        self.assertEqual(fresh.sr_lead_notes, "newer\n\nOCR block")
        fresh.add_comment.assert_called_once_with("Comment", "OCR block")
