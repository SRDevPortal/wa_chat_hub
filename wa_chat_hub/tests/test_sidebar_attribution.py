from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.api.chat import _get_sidebar_attribution
from wa_chat_hub.messaging.attribution import extract_attribution, get_conversation_attribution


class TestSidebarAttribution(TestCase):
    def setUp(self):
        self.convo = SimpleNamespace(name="CHAT-1", linked_crm_lead="LEAD-1", source="Vobiz")
        self.lead = {
            "sr_w_source_id": "ad-123", "sr_w_source_url": "https://example.com/ad",
            "sr_w_ctwa_clid": "click-123", "source": "Meta",
        }
        self.api_patch = patch("wa_chat_hub.api.chat.frappe")
        self.api = self.api_patch.start()
        self.addCleanup(self.api_patch.stop)
        self.api.get_meta.return_value.has_field.return_value = True
        self.api.db.get_value.return_value = self.lead
        self.access_patch = patch("wa_chat_hub.api.chat.can_read_crm_lead", return_value=True)
        self.access = self.access_patch.start()
        self.addCleanup(self.access_patch.stop)
        self.history_patch = patch("wa_chat_hub.api.chat.get_conversation_attribution", return_value={})
        self.history = self.history_patch.start()
        self.addCleanup(self.history_patch.stop)

    def test_source_only_conversation_fills_all_ad_fields_from_linked_lead(self):
        before = deepcopy(vars(self.convo))
        window = {"ctwa_active": False}
        data = _get_sidebar_attribution(self.convo, window)
        self.assertEqual(data, {
            "source": "Vobiz", "source_id": "ad-123",
            "source_url": "https://example.com/ad", "ctwa_clid": "click-123",
        })
        self.api.db.get_value.assert_called_once()
        self.assertEqual(set(self.api.db.get_value.call_args.args[2]),
                         {"sr_w_source_id", "sr_w_source_url", "sr_w_ctwa_clid"})
        self.history.assert_not_called()
        self.api.db.set_value.assert_not_called()
        self.assertEqual(vars(self.convo), before)
        self.assertEqual(window, {"ctwa_active": False})

    def test_complete_conversation_needs_no_lead_or_message_read(self):
        self.convo.source_id = "conversation-ad"
        self.convo.source_url = "https://example.com/current"
        self.convo.ctwa_clid = "conversation-click"
        data = _get_sidebar_attribution(self.convo, {})
        self.assertEqual(data["source_id"], "conversation-ad")
        self.api.db.get_value.assert_not_called()
        self.access.assert_not_called()
        self.history.assert_not_called()

    def test_existing_fields_win_and_missing_window_fields_are_used_individually(self):
        self.convo.source_id = "conversation-ad"
        data = _get_sidebar_attribution(self.convo, {"source_url": "https://example.com/window"})
        self.assertEqual(data["source_id"], "conversation-ad")
        self.assertEqual(data["source_url"], "https://example.com/window")
        self.assertEqual(self.api.db.get_value.call_args.args[2], ["sr_w_ctwa_clid"])

    def test_legacy_link_to_crm_lead_is_supported(self):
        self.convo.linked_crm_lead = None
        self.convo.linked_reference_doctype = "CRM Lead"
        self.convo.linked_reference_name = "LEGACY-LEAD"
        _get_sidebar_attribution(self.convo, {})
        self.api.db.get_value.assert_called_once()
        self.assertEqual(self.api.db.get_value.call_args.args[1], "LEGACY-LEAD")

    def test_inaccessible_lead_values_are_not_exposed(self):
        self.access.return_value = False
        data = _get_sidebar_attribution(self.convo, {})
        self.assertIsNone(data["source_id"])
        self.api.db.get_value.assert_not_called()
        self.history.assert_called_once_with("CHAT-1")

    def test_missing_optional_crm_fields_use_legacy_payload_without_invalid_query(self):
        self.api.get_meta.return_value.has_field.return_value = False
        self.history.return_value = {"source_id": "legacy-ad"}
        data = _get_sidebar_attribution(self.convo, {})
        self.assertEqual(data["source_id"], "legacy-ad")
        self.api.db.get_value.assert_not_called()

    def test_unlinked_chat_does_not_search_leads_by_phone(self):
        self.convo.linked_crm_lead = None
        _get_sidebar_attribution(self.convo, {})
        self.api.db.get_value.assert_not_called()
        self.access.assert_not_called()
        self.history.assert_called_once()


class TestLegacyAttribution(TestCase):
    def test_source_only_recent_payload_does_not_hide_older_ad_fields(self):
        rows = [
            frappe._dict(raw_payload={"_internal_lead_source": "Vobiz"}),
            frappe._dict(raw_payload={"referral": {
                "source_id": "ad-123", "source_url": "https://example.com/ad",
                "ctwa_clid": "click-123", "source": "Meta",
            }}),
        ]
        with patch("wa_chat_hub.messaging.attribution.safe_ai_get_all", return_value=rows) as query:
            data = get_conversation_attribution("CHAT-1")
        self.assertEqual(data["source"], "Vobiz")
        self.assertEqual(data["source_id"], "ad-123")
        self.assertEqual(data["ctwa_clid"], "click-123")
        query.assert_called_once()
        self.assertEqual(query.call_args.kwargs["limit_page_length"], 20)

    def test_invalid_payload_and_empty_history_are_safe(self):
        for rows in ([], [frappe._dict(raw_payload="not json")]):
            with self.subTest(rows=rows), patch("wa_chat_hub.messaging.attribution.safe_ai_get_all", return_value=rows):
                self.assertEqual(get_conversation_attribution("CHAT-1"), {})

    def test_payload_field_aliases_are_normalized_on_both_sides(self):
        for keys in (("source_id", "source_url", "ctwa_clid"),
                     ("sourceId", "sourceUrl", "ctwaClid"),
                     ("Source ID", "Source URL", "ctwa clid")):
            with self.subTest(keys=keys):
                data = extract_attribution(dict(zip(keys, ("ad-123", "https://example.com/ad", "click-123"))))
                self.assertEqual(data["source_id"], "ad-123")
                self.assertEqual(data["source_url"], "https://example.com/ad")
                self.assertEqual(data["ctwa_clid"], "click-123")
