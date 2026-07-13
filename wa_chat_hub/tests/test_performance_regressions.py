from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.ai.lead_scoring import ScoreResult, sync_to_conversation
from wa_chat_hub.maintenance.phone_backfill import normalized_values
from wa_chat_hub.messaging.idempotency import (
    build_message_dedupe_key,
    webhook_idempotency_enabled,
)
from wa_chat_hub.messaging.windows import get_messaging_window_state
from wa_chat_hub.services import _available_phone_index_filters, _phone_rows


class _Meta:
    def __init__(self, fields):
        self.fields = set(fields)

    def has_field(self, fieldname):
        return fieldname in self.fields


class TestPhoneLookupPerformance(TestCase):
    def test_uses_normalized_index_fields_without_duplicates(self):
        meta = _Meta(
            {
                "vobiz_normalized_phone",
                "sr_mobile_norm",
                "vobiz_mobile_last10",
                "vobiz_phone_last10",
            }
        )

        filters = _available_phone_index_filters(
            meta,
            ["mobile_no", "mobile", "phone"],
            "+91 98765-43210",
        )

        self.assertEqual(
            filters,
            [
                ("vobiz_normalized_phone", "919876543210"),
                ("sr_mobile_norm", "9876543210"),
                ("vobiz_mobile_last10", "9876543210"),
                ("vobiz_phone_last10", "9876543210"),
            ],
        )

    @patch("wa_chat_hub.services.safe_ai_get_all")
    def test_phone_query_is_exact_not_wildcard(self, get_all):
        _phone_rows("CRM Lead", "vobiz_mobile_last10", "9876543210", limit=1)

        _, kwargs = get_all.call_args
        self.assertEqual(kwargs["filters"], {"vobiz_mobile_last10": "9876543210"})
        self.assertNotIn("like", str(kwargs["filters"]).lower())
        self.assertEqual(kwargs["order_by"], "modified desc")


class TestPhoneNormalizationBackfill(TestCase):
    def test_builds_all_available_phone_keys(self):
        values = normalized_values(
            {
                "mobile_no": "+91 98765-43210",
                "phone": "011 4567 8901",
                "custom_whatsapp_number": "0091-99887-76655",
            },
            [
                "vobiz_normalized_phone",
                "vobiz_mobile_last10",
                "vobiz_phone_last10",
                "vobiz_whatsapp_last10",
                "sr_mobile_norm",
            ],
        )

        self.assertEqual(values["vobiz_normalized_phone"], "919876543210")
        self.assertEqual(values["vobiz_mobile_last10"], "9876543210")
        self.assertEqual(values["vobiz_phone_last10"], "1145678901")
        self.assertEqual(values["vobiz_whatsapp_last10"], "9988776655")
        self.assertEqual(values["sr_mobile_norm"], "9876543210")


class TestMessageIdempotency(TestCase):
    def test_key_is_stable_and_scoped_to_conversation(self):
        first = build_message_dedupe_key(
            conversation="CONV-1",
            provider_name="Meta",
            provider_message_id="wamid.123",
            channel_message_id=None,
        )
        same = build_message_dedupe_key(
            conversation="CONV-1",
            provider_name="meta",
            provider_message_id="wamid.123",
            channel_message_id=None,
        )
        other_conversation = build_message_dedupe_key(
            conversation="CONV-2",
            provider_name="Meta",
            provider_message_id="wamid.123",
            channel_message_id=None,
        )

        self.assertEqual(first, same)
        self.assertNotEqual(first, other_conversation)
        self.assertEqual(len(first), 64)

    def test_provider_event_id_is_a_valid_fallback_identity(self):
        key = build_message_dedupe_key(
            conversation="CONV-1",
            provider_name="Meta",
            provider_message_id=None,
            channel_message_id=None,
            provider_event_id="event-123",
        )

        self.assertIsNotNone(key)

    @patch("wa_chat_hub.messaging.idempotency.frappe.get_cached_doc")
    def test_rollout_flag_can_disable_idempotency(self, get_settings):
        get_settings.return_value = SimpleNamespace(
            meta=_Meta({"enable_webhook_idempotency"}),
            enable_webhook_idempotency=0,
        )

        self.assertFalse(webhook_idempotency_enabled())


class TestMessagingWindowPerformance(TestCase):
    @patch("wa_chat_hub.messaging.windows._messaging_window_fields_ready", return_value=True)
    @patch("wa_chat_hub.messaging.windows._ensure_messaging_window_schema")
    @patch("wa_chat_hub.messaging.windows._last_customer_inbound_at")
    def test_runtime_state_does_not_scan_message_history(self, history_lookup, _ensure, _ready):
        last_at = datetime(2026, 7, 13, 10, 0, 0)
        convo = SimpleNamespace(
            last_customer_message_at=last_at,
            customer_service_window_expires_at=last_at + timedelta(hours=24),
            ctwa_clid=None,
            ctwa_window_expires_at=None,
            messaging_window_mode="free_form",
        )

        state = get_messaging_window_state(
            "CONV-1",
            now=last_at + timedelta(hours=1),
            convo=convo,
        )

        history_lookup.assert_not_called()
        self.assertTrue(state["customer_service_active"])
        self.assertEqual(state["mode"], "free_form")


class TestLeadScoringWrites(TestCase):
    @patch("wa_chat_hub.ai.lead_scoring.with_db_lock_retry")
    @patch("wa_chat_hub.ai.lead_scoring.assert_ai_doctype_permission")
    def test_score_update_is_conditional(self, assert_permission, retry):
        retry.side_effect = lambda _label, action: action()
        result = ScoreResult(75, "Hot", "Hindi", "test")

        with patch.object(frappe.db, "sql") as sql:
            sync_to_conversation("CONV-1", result)

        assert_permission.assert_called_once_with("Chat Conversation", "write")
        query = sql.call_args.args[0]
        self.assertIn("NOT (lead_score <=> %s)", query)
        self.assertIn("NOT (lead_temperature <=> %s)", query)
        self.assertIn("NOT (lead_lan <=> %s)", query)
