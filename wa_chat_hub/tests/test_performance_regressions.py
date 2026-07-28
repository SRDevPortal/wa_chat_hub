from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.ai.lead_scoring import ScoreResult, sync_to_conversation
from wa_chat_hub.api.chat import (
    CONVERSATION_SEARCH_CANDIDATE_LIMIT,
    _bounded_conversation_search_names,
    _indexed_reference_phone_names,
    _find_existing_conversation_by_phone,
    _matching_contact_names,
    _phone_search_value,
    search_conversations,
)
from wa_chat_hub.maintenance.phone_backfill import normalized_values
from wa_chat_hub.messaging.idempotency import (
    build_message_dedupe_key,
    webhook_idempotency_enabled,
)
from wa_chat_hub.messaging.windows import get_messaging_window_state
from wa_chat_hub.services import _available_phone_index_filters, _phone_rows
from wa_chat_hub.services import _find_indexed_phone_match_names


class _Meta:
    def __init__(self, fields):
        self.fields = set(fields)

    def has_field(self, fieldname):
        return fieldname in self.fields


class TestPhoneLookupPerformance(TestCase):
    @patch("wa_chat_hub.services.assert_ai_doctype_permission")
    @patch("wa_chat_hub.services.safe_ai_exists", return_value=True)
    @patch("wa_chat_hub.services.frappe.get_meta")
    @patch("wa_chat_hub.services._indexed_phone_rows")
    def test_patient_identity_lookup_is_exact_bounded_and_detects_ambiguity(
        self,
        indexed_rows,
        get_meta,
        _exists,
        _permission,
    ):
        get_meta.return_value = _Meta(
            {"vobiz_normalized_phone", "vobiz_mobile_last10"}
        )
        indexed_rows.side_effect = [
            [frappe._dict(name="PAT-1"), frappe._dict(name="PAT-2")],
        ]

        matches = _find_indexed_phone_match_names(
            "Patient",
            ["mobile", "phone"],
            "+91 98765-43210",
            limit=2,
        )

        self.assertEqual(matches, {"PAT-1", "PAT-2"})
        self.assertEqual(indexed_rows.call_args.kwargs["limit"], 2)

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


class TestConversationSearchPerformance(TestCase):
    def test_alphanumeric_text_is_not_misclassified_as_phone(self):
        self.assertEqual(_phone_search_value("Lead 2"), "")

    @patch("wa_chat_hub.api.chat.normalize_phone", return_value="919876543210")
    @patch("wa_chat_hub.api.chat.frappe.get_all", return_value=["CONTACT-1"])
    def test_contact_phone_search_uses_exact_lookup_first(self, get_all, _normalize):
        names = _matching_contact_names("+91 98765-43210")

        self.assertEqual(names, ["CONTACT-1"])
        _, kwargs = get_all.call_args
        self.assertEqual(kwargs["filters"], {"phone_number": "919876543210"})
        self.assertNotIn("like", str(kwargs).lower())

    @patch("wa_chat_hub.api.chat.normalize_phone", return_value="919876543210")
    @patch("wa_chat_hub.api.chat.frappe.get_all", return_value=[])
    def test_missing_contact_phone_does_not_fall_back_to_full_scan(self, get_all, _normalize):
        self.assertEqual(_matching_contact_names("+91 98765-43210"), [])

        get_all.assert_called_once()
        self.assertNotIn("like", str(get_all.call_args.kwargs).lower())

    @patch("wa_chat_hub.api.chat.frappe.db.get_value", return_value="CONV-1")
    @patch("wa_chat_hub.api.chat.frappe.get_all", return_value=["CONTACT-1"])
    def test_existing_conversation_phone_lookup_is_exact(self, get_all, _get_value):
        self.assertEqual(_find_existing_conversation_by_phone("+91 98765-43210"), "CONV-1")

        get_all.assert_called_once()
        self.assertEqual(get_all.call_args.kwargs["filters"], {"phone_number": "919876543210"})
        self.assertNotIn("like", str(get_all.call_args.kwargs).lower())

    @patch("wa_chat_hub.api.chat.frappe.get_meta")
    @patch("wa_chat_hub.api.chat.frappe.get_all", return_value=["LEAD-1"])
    def test_reference_phone_search_uses_normalized_index_fields(self, get_all, get_meta):
        get_meta.return_value = _Meta({"vobiz_normalized_phone", "vobiz_mobile_last10"})

        names = _indexed_reference_phone_names(
            "CRM Lead",
            ["mobile_no", "phone"],
            "+91 98765-43210",
        )

        self.assertEqual(names, ["LEAD-1"])
        self.assertTrue(get_all.called)
        for call in get_all.call_args_list:
            self.assertNotIn("like", str(call.kwargs.get("filters")).lower())

    @patch("wa_chat_hub.api.chat._force_scoped_reference_doctype", return_value=None)
    @patch("wa_chat_hub.api.chat.frappe.get_all")
    def test_single_character_text_search_is_rejected_before_querying(self, get_all, _scope):
        self.assertEqual(search_conversations("a"), {"success": True, "result": []})
        get_all.assert_not_called()

    @patch("wa_chat_hub.api.chat._force_scoped_reference_doctype", return_value=None)
    @patch("wa_chat_hub.api.chat.frappe.get_all")
    def test_short_phone_search_is_rejected_before_querying(self, get_all, _scope):
        self.assertEqual(search_conversations("123"), {"success": True, "result": []})
        get_all.assert_not_called()

    @patch("wa_chat_hub.api.chat._force_scoped_reference_doctype", return_value=None)
    @patch("wa_chat_hub.api.chat._short_cache_get", return_value=[{"name": "CONV-1"}])
    @patch("wa_chat_hub.api.chat.frappe.get_all")
    def test_repeated_search_uses_user_scoped_short_cache(self, get_all, _cache_get, _scope):
        result = search_conversations("Alice")

        self.assertEqual(result["result"], [{"name": "CONV-1"}])
        get_all.assert_not_called()

    @patch("wa_chat_hub.api.chat._short_cache_set")
    @patch("wa_chat_hub.api.chat._bounded_conversation_search_names", return_value=set())
    @patch("wa_chat_hub.api.chat._conversation_list_filters", return_value={})
    @patch("wa_chat_hub.api.chat._short_cache_get", return_value=None)
    @patch("wa_chat_hub.api.chat._force_scoped_reference_doctype", return_value=None)
    def test_text_search_uses_bounded_candidate_query(
        self,
        _scope,
        _cache_get,
        _filters,
        bounded_search,
        cache_set,
    ):
        self.assertEqual(search_conversations("Alice"), {"success": True, "result": []})

        bounded_search.assert_called_once_with("Alice", {}, 100)
        cache_set.assert_called_once()

    @patch("wa_chat_hub.api.chat.conversation_access_sql_condition", return_value="1=1")
    @patch("wa_chat_hub.api.chat._has_conversation_last_message_time", return_value=True)
    @patch("wa_chat_hub.api.chat.frappe.get_meta")
    @patch("wa_chat_hub.api.chat.frappe.db.exists", return_value=True)
    @patch("wa_chat_hub.api.chat.frappe.db.sql", return_value=["CONV-1"])
    def test_candidate_query_limits_rows_before_wildcard_joins(
        self,
        sql,
        _exists,
        get_meta,
        _last_message_time,
        _access,
    ):
        get_meta.side_effect = lambda doctype: _Meta(
            {
                "linked_crm_lead",
                "lead_name",
                "email",
                "mobile_no",
                "patient_name",
                "sr_patient_id",
                "mobile",
            }
        )

        names = _bounded_conversation_search_names("Alice", {"status": "Open"}, 100)

        self.assertEqual(names, {"CONV-1"})
        query = sql.call_args.args[0]
        values = sql.call_args.args[1]
        self.assertLess(query.index("limit %(candidate_limit)s"), query.index("left join `tabChat Contact`"))
        self.assertEqual(values["candidate_limit"], CONVERSATION_SEARCH_CANDIDATE_LIMIT)
        self.assertEqual(values["query"], "%Alice%")
        self.assertNotIn("%Alice%", query)
        self.assertTrue(sql.call_args.kwargs["pluck"])

    def test_candidate_query_executes_against_database(self):
        names = _bounded_conversation_search_names("codex-search-probe-91e7", {}, 5)

        self.assertIsInstance(names, set)


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
    @patch("wa_chat_hub.messaging.windows._last_customer_inbound_at")
    def test_runtime_state_does_not_scan_message_history(self, history_lookup, _ready):
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

    @patch("wa_chat_hub.messaging.windows.frappe.clear_cache")
    @patch("wa_chat_hub.messaging.windows.frappe.reload_doc")
    @patch(
        "wa_chat_hub.messaging.windows._fallback_window_state_from_messages",
        return_value={"schema_pending": True},
    )
    @patch("wa_chat_hub.messaging.windows._messaging_window_fields_ready", return_value=False)
    def test_schema_pending_uses_fallback_without_schema_mutation(
        self,
        _ready,
        fallback,
        reload_doc,
        clear_cache,
    ):
        now = datetime(2026, 7, 14, 10, 30, 0)

        state = get_messaging_window_state("CONV-1", now=now)

        self.assertEqual(state, {"schema_pending": True})
        fallback.assert_called_once_with("CONV-1", now)
        reload_doc.assert_not_called()
        clear_cache.assert_not_called()


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
