from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from wa_chat_hub.api.ai_bot import (
    _autopilot_batch_already_answered,
    schedule_autopilot_for_message,
    _wait_for_conversation_batch_window,
    process_conversation,
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


    def test_wait_refreshes_snapshot_to_collect_new_messages(self):
        for content_type in ("Text", "Image", "Audio", "Video", "Document"):
            with self.subTest(content_type=content_type):
                first = SimpleNamespace(name="first", content_type=content_type, media_url="https://example.test/1")
                second = SimpleNamespace(name="second", content_type=content_type, media_url="https://example.test/2")
                live = [first]
                snapshot = [first]
                db = MagicMock()
                db.rollback.side_effect = lambda: snapshot.__setitem__(0, live[0])
                def receive_during_wait(seconds):
                    live[0] = second
                settings = SimpleNamespace(enable_autopilot_reply_batching=1,
                    text_autopilot_reply_delay_seconds=5, media_autopilot_reply_delay_seconds=15)
                with patch("wa_chat_hub.api.ai_bot.frappe.db", db, create=True), \
                     patch("wa_chat_hub.api.ai_bot.frappe.flags", SimpleNamespace(in_test=False), create=True), \
                     patch("wa_chat_hub.api.ai_bot.time.sleep", side_effect=receive_during_wait), \
                     patch("wa_chat_hub.api.ai_bot._latest_autopilot_inbound", side_effect=lambda _: snapshot[0]):
                    selected, waited = _wait_for_conversation_batch_window("243", settings)
                self.assertIs(selected, second)
                self.assertGreater(waited, 0)
                self.assertGreaterEqual(db.rollback.call_count, 3)

    def test_worker_refreshes_under_shared_lock_before_answer_check(self):
        events = []
        db = MagicMock()
        db.rollback.side_effect = lambda: events.append("refresh")
        cache = MagicMock()
        cache.make_key.return_value = b"site|batch-lock"
        cache.lock.return_value.__enter__.side_effect = lambda: events.append("locked")
        latest = SimpleNamespace(name="last-image")
        with patch("wa_chat_hub.api.ai_bot.frappe.db", db, create=True), \
             patch("wa_chat_hub.api.ai_bot.frappe.cache", return_value=cache), \
             patch("wa_chat_hub.api.ai_bot.frappe.get_single", return_value=SimpleNamespace(enable_ai_autopilot=1, autopilot_mode="Limited Auto Reply")), \
             patch("wa_chat_hub.api.ai_bot.set_service_user_context"), \
             patch("wa_chat_hub.api.ai_bot.set_ai_security_context"), \
             patch("wa_chat_hub.api.ai_bot.assert_ai_doctype_permission"), \
             patch("wa_chat_hub.api.ai_bot._conversation_allows_autopilot", return_value=True), \
             patch("wa_chat_hub.api.ai_bot._wait_for_conversation_batch_window", return_value=(latest, 20)), \
             patch("wa_chat_hub.api.ai_bot._latest_autopilot_inbound", side_effect=lambda _: events.append("latest") or latest), \
             patch("wa_chat_hub.api.ai_bot._autopilot_batch_already_answered", side_effect=lambda *_: events.append("answered") or True), \
             patch("wa_chat_hub.api.ai_bot._log_ai_timing"), \
             patch("wa_chat_hub.api.ai_bot.process_message") as process:
            process_conversation("243", trigger_message_id="first-image")
        self.assertEqual(events, ["locked", "refresh", "latest", "answered"])
        cache.lock.assert_called_once_with(b"site|batch-lock", timeout=900, blocking_timeout=180)
        process.assert_not_called()

    @patch("wa_chat_hub.api.ai_bot.safe_ai_get_all")
    @patch("wa_chat_hub.api.ai_bot._autopilot_batch_window_start")
    def test_failed_and_pending_replies_allow_batch_retry(self, batch_start, get_all):
        batch_start.return_value = "2026-10-08 09:27:00"
        for status in ("Failed", "Pending"):
            with self.subTest(status=status):
                get_all.return_value = [SimpleNamespace(delivery_status=status)]
                self.assertFalse(_autopilot_batch_already_answered("243",
                    SimpleNamespace(creation="2026-10-08 09:27:23")))


    def test_single_image_and_image_followed_by_text_select_latest(self):
        image = SimpleNamespace(name="image", content_type="Image", media_url="https://example.test/image")
        text = SimpleNamespace(name="text", content_type="Text", media_url=None)
        settings = SimpleNamespace(enable_autopilot_reply_batching=1,
            text_autopilot_reply_delay_seconds=5, media_autopilot_reply_delay_seconds=15)
        for arrivals, expected in (([image, image, image], image), ([image, text, text], text)):
            with self.subTest(expected=expected.name), \
                 patch("wa_chat_hub.api.ai_bot.frappe.db", MagicMock(), create=True), \
                 patch("wa_chat_hub.api.ai_bot.frappe.flags", SimpleNamespace(in_test=True), create=True), \
                 patch("wa_chat_hub.api.ai_bot._latest_autopilot_inbound", side_effect=arrivals):
                selected, _ = _wait_for_conversation_batch_window("243", settings)
                self.assertIs(selected, expected)
