from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe
from wa_chat_hub.job_queues import ai_queue
from wa_chat_hub.api.ai_bot import _remaining_batch_delay


class WorkerQueueTests(TestCase):
    def test_default_and_opt_in_queue(self):
        for conf, expected in (({}, 'short'), ({'wa_ai_queue':'wa_ai'}, 'wa_ai')):
            with patch.object(frappe, 'local', SimpleNamespace(conf=conf)):
                self.assertEqual(ai_queue(), expected)

    def test_invalid_queue_fails_explicitly(self):
        with patch.object(frappe, 'local', SimpleNamespace(conf={'wa_ai_queue':'invalid queue'})):
            with self.assertRaises(ValueError):
                ai_queue()

    def test_existing_queue_wait_is_not_paid_again(self):
        now = datetime(2026, 11, 16, 12)
        with patch.object(frappe.utils, 'now_datetime', return_value=now):
            self.assertEqual(_remaining_batch_delay(SimpleNamespace(creation=now-timedelta(seconds=300)), 10), 0)
            self.assertEqual(_remaining_batch_delay(SimpleNamespace(creation=now-timedelta(seconds=3)), 10), 7)
            self.assertEqual(_remaining_batch_delay(SimpleNamespace(creation=now+timedelta(seconds=3)), 10), 10)

    def test_missing_timestamp_preserves_delay(self):
        self.assertEqual(_remaining_batch_delay(SimpleNamespace(), 5), 5)
