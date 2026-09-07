from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.api.ai_bot import (
    _autopilot_batch_already_answered,
    schedule_autopilot_for_message,
)


class TestAIAutopilotQueue(TestCase):
    @patch("wa_chat_hub.api.ai_bot.safe_ai_get_all", return_value=[])
    @patch("wa_chat_hub.api.ai_bot._autopilot_batch_window_start")
    def test_audio_after_ai_reply_starts_a_new_reply_cycle(self, batch_start, _get_all):
        batch_start.return_value = "2026-09-07 17:26:01"
        current_audio = SimpleNamespace(
            name="1244",
            creation="2026-09-07 17:26:45",
            content_type="Audio",
            media_url="https://example.test/new-audio.ogg",
        )

        self.assertFalse(_autopilot_batch_already_answered("243", current_audio))

    @patch("wa_chat_hub.api.ai_bot.safe_ai_get_all")
    @patch("wa_chat_hub.api.ai_bot._autopilot_batch_window_start")
    def test_audio_batch_is_skipped_after_one_combined_reply(self, batch_start, get_all):
        batch_start.return_value = "2026-09-07 17:25:39"
        get_all.return_value = [
            SimpleNamespace(
                name="1243",
                delivery_status="Sent",
                sender_type="AI",
                creation="2026-09-07 17:26:01",
            )
        ]
        latest_audio = SimpleNamespace(
            name="1242",
            creation="2026-09-07 17:25:55",
            content_type="Audio",
            media_url="https://example.test/second-audio.ogg",
        )

        self.assertTrue(_autopilot_batch_already_answered("243", latest_audio))

    @patch("wa_chat_hub.api.ai_bot._log_ai_timing")
    @patch("wa_chat_hub.api.ai_bot.assert_ai_doctype_permission")
    @patch("wa_chat_hub.api.ai_bot.enqueue")
    @patch("wa_chat_hub.api.ai_bot.frappe.get_single")
    @patch("wa_chat_hub.api.ai_bot.safe_ai_get_doc")
    @patch("wa_chat_hub.api.ai_bot.safe_ai_exists", return_value=True)
    def test_each_inbound_message_gets_a_distinct_batch_job(
        self,
        _exists,
        get_doc,
        get_single,
        enqueue,
        _permission,
        _timing,
    ):
        get_single.return_value = SimpleNamespace(
            enable_ai_autopilot=1,
            autopilot_mode="Limited Auto Reply",
            enable_autopilot_reply_batching=1,
        )
        get_doc.side_effect = [
            SimpleNamespace(
                name="1233",
                conversation="243",
                direction="Inbound",
                sender_type="Customer",
                content_type="Audio",
                media_url="https://example.test/first.ogg",
                body="[Audio message received]",
            ),
            SimpleNamespace(
                name="1234",
                conversation="243",
                direction="Inbound",
                sender_type="Customer",
                content_type="Audio",
                media_url="https://example.test/second.ogg",
                body="[Audio message received]",
            ),
        ]

        schedule_autopilot_for_message("1233")
        schedule_autopilot_for_message("1234")

        first = enqueue.call_args_list[0].kwargs
        second = enqueue.call_args_list[1].kwargs
        self.assertEqual(first["job_id"], "wa_ai_autopilot_conversation_243_1233")
        self.assertEqual(second["job_id"], "wa_ai_autopilot_conversation_243_1234")
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertTrue(first["deduplicate"])
        self.assertTrue(second["deduplicate"])
