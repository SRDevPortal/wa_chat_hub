import json
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from wa_chat_hub.ai import lead_scoring
from wa_chat_hub.ai.scoring_policy import evaluate, validate_scoring_policy
from wa_chat_hub import lead_ai, services
from wa_chat_hub.policy import PolicyBundle


POLICY = {
    "enabled": True, "base_score": 20, "inbound_message_weight": 3,
    "inbound_message_cap": 30, "unread_weight": 2, "unread_cap": 15,
    "priority_weights": {"Medium": 5}, "hot_terms": ["doctor"], "hot_term_weight": 4,
    "warm_terms": ["details"], "warm_term_weight": 2,
    "temperature_bands": [{"minimum": 70, "label": "Hot"}, {"minimum": 40, "label": "Warm"},
                          {"minimum": 0, "label": "Cold"}],
    "disease_media_scoring": {"enabled": True, "direction": "Inbound", "media_types": ["Image", "Document", "Video", "Audio", "Voice"],
        "require_disease_related_media": False, "disease_related_media_weight": 70, "temperature_override": "Hot"},
}


def message(name="M", direction="Inbound", body="hello", content_type="Text", **kw):
    return dict(name=name, direction=direction, body=body, content_type=content_type, **kw)


class ScoringPolicyTests(TestCase):
    def test_existing_text_weights_and_caps_are_preserved(self):
        score, label, details = evaluate(POLICY, [message()], unread_count=1, priority="Medium")
        self.assertEqual((score, label), (30, "Cold"))
        score, _, details = evaluate(POLICY, [message(body="doctor details")] * 20, unread_count=50, priority="Medium")
        self.assertEqual(score, 76)
        self.assertEqual(details["contributions"]["inbound"], 30)
        self.assertEqual(details["contributions"]["unread"], 15)

    def test_sriaas_document_receives_configured_bonus_once(self):
        rows = [message(body="doctor"), message("DOC", content_type="Document", media_url="/private/files/a.pdf")]
        score, label, details = evaluate(POLICY, rows, priority="Medium")
        self.assertEqual((score, label), (100, "Hot"))
        self.assertEqual(details["contributions"]["media"], 70)
        rows.append(message("DOC2", content_type="Image", attachment_file="FILE"))
        self.assertEqual(evaluate(POLICY, rows)[2]["contributions"]["media"], 70)

    def test_media_direction_type_and_actual_attachment_are_required(self):
        for row in [message(content_type="Image"), message(content_type="Sticker", media_url="x"),
                    message(direction="Outbound", content_type="Image", media_url="x")]:
            self.assertEqual(evaluate(POLICY, [row])[2]["contributions"]["media"], 0)

    def test_disabled_media_and_changed_bonus_follow_policy(self):
        p = deepcopy(POLICY)
        row = message(content_type="Image", media_url="x")
        p["disease_media_scoring"]["enabled"] = False
        self.assertEqual(evaluate(p, [row])[0:2], (23, "Cold"))
        p["disease_media_scoring"].update(enabled=True, disease_related_media_weight=2, temperature_override="Warm")
        self.assertEqual(evaluate(p, [row])[0:2], (25, "Warm"))

    def test_disease_requirement_uses_explicit_terms_and_derived_text(self):
        p = deepcopy(POLICY)
        p["disease_media_scoring"].update(require_disease_related_media=True, disease_terms=["रिपोर्ट"])
        row = message(content_type="Image", media_url="x")
        self.assertEqual(evaluate(p, [row])[0], 23)
        row["scoring_media_text"] = "रिपोर्ट"
        self.assertEqual(evaluate(p, [row])[1], "Hot")

    def test_voice_alias(self):
        p = deepcopy(POLICY)
        p["disease_media_scoring"]["media_types"] = ["Voice"]
        self.assertEqual(evaluate(p, [message(content_type="Audio", media_url="x")])[1], "Hot")

    def test_stopped_conversation_uses_policy_band_and_no_media_override(self):
        p = deepcopy(POLICY)
        p["stopped_score"] = 42
        self.assertEqual(evaluate(p, [message(content_type="Image", media_url="x")], stopped=True)[:2], (42, "Warm"))

    def test_keyword_direction_is_configurable_without_changing_default(self):
        p = deepcopy(POLICY)
        rows = [message(direction="Outbound", body="doctor")]
        self.assertEqual(evaluate(p, rows)[0], 24)
        p["keyword_direction"] = "Inbound"
        self.assertEqual(evaluate(p, rows)[0], 20)

    def test_threshold_boundaries_are_policy_defined(self):
        for base, expected in [(39, "Cold"), (40, "Warm"), (69, "Warm"), (70, "Hot")]:
            p = deepcopy(POLICY); p["base_score"] = base
            self.assertEqual(evaluate(p, [])[1], expected)

    def test_invalid_or_unsupported_configuration_is_not_ignored(self):
        for update in [{"unknown_rule": 2}, {"base_score": float("nan")}, {"enabled": "false"},
                       {"hot_terms": "doctor"}, {"history_limit": -1}, {"temperature_bands": []},
                       {"disease_media_scoring": {"enabled": True, "media_types": ["Image"], "require_disease_related_media": True}}]:
            p = deepcopy(POLICY); p.update(update)
            with self.subTest(update=update), self.assertRaises(ValueError):
                validate_scoring_policy(p)

    def test_legacy_settings_are_explicitly_flagged_not_double_counted(self):
        p = deepcopy(POLICY); p["legacy_auto_update"] = {"base_score": 90}
        score, _, details = evaluate(p, [])
        self.assertEqual(score, 20)
        self.assertIn("deprecated", details["warnings"][0])


class ScoringIntegrationTests(TestCase):
    def setUp(self):
        self.convo = SimpleNamespace(name="C", channel_account="A", unread_count=0, priority="Medium",
                                     lead_score=0, lead_temperature="Cold", lead_lan="English", conversation_stopped=0)
        self.bundle = PolicyBundle("Configured", 6, {"lead_scoring_policy": deepcopy(POLICY)})

    def test_media_analysis_for_unlinked_chat_requires_policy_evidence_condition(self):
        with patch.object(lead_scoring, "get_conversation_policy", return_value=self.bundle):
            self.assertFalse(lead_scoring.needs_media_scoring_text(self.convo))
            self.bundle.sections["lead_scoring_policy"]["disease_media_scoring"].update(
                require_disease_related_media=True, disease_terms=["report"])
            self.assertTrue(lead_scoring.needs_media_scoring_text(self.convo))
            self.bundle.sections["lead_scoring_policy"]["enabled"] = False
            self.assertFalse(lead_scoring.needs_media_scoring_text(self.convo))

    def test_full_history_is_paginated_without_losing_last_attachment(self):
        newest = message("MEDIA", content_type="Image", media_url="x")
        with patch.object(lead_scoring, "safe_ai_get_doc", return_value=self.convo), \
             patch.object(lead_scoring, "safe_ai_get_all", side_effect=[[newest] + [message()] * 499, [message()]]) as read, \
             patch.object(lead_scoring, "get_conversation_policy", return_value=self.bundle), \
             patch.object(lead_scoring, "resolve_language_from_history", return_value={"label": "English"}):
            result = lead_scoring.recompute_conversation_metrics("C")
        self.assertEqual(result.details["history_count"], 501)
        self.assertEqual(result.details["media_message_ids"], ["MEDIA"])
        self.assertEqual([c.kwargs["limit_start"] for c in read.call_args_list], [0, 500])

    def test_unchanged_media_evidence_does_not_enqueue_again(self):
        db = MagicMock()
        with patch.object(frappe, "db", db), patch.object(lead_scoring, "assert_ai_doctype_permission"), \
             patch.object(lead_scoring, "safe_ai_get_doc", return_value=SimpleNamespace(conversation="C", scoring_media_text="same")), \
             patch.object(lead_ai, "_schedule") as schedule:
            lead_scoring.record_media_scoring_text("M", "same")
        db.set_value.assert_not_called(); schedule.assert_not_called()

    def test_insert_update_hook_does_not_duplicate_after_insert_job(self):
        doc = MagicMock(flags=SimpleNamespace(in_insert=True))
        with patch.object(lead_ai, "_schedule") as schedule:
            lead_ai.on_message_update(doc)
        schedule.assert_not_called()

    def test_policy_save_rejects_unknown_rule_and_warns_about_legacy(self):
        from wa_chat_hub.wa_chat_hub.doctype.wa_ai_policy_bundle.wa_ai_policy_bundle import WAAIPolicyBundle
        doc = WAAIPolicyBundle({"doctype": "WA AI Policy Bundle", "policy_version": 6,
                               "lead_scoring_policy": {**POLICY, "typo": 10}})
        with patch.object(frappe, "throw", side_effect=frappe.ValidationError):
            with self.assertRaises(frappe.ValidationError):
                doc.validate()
        doc.lead_scoring_policy = {**POLICY, "legacy_auto_update": {"base_score": 35}}
        with patch.object(frappe, "msgprint") as warning:
            doc.validate()
        warning.assert_called_once()

    def test_history_after_first_40_is_scored_and_version_recorded(self):
        rows = [message(str(i)) for i in range(45)]
        rows.append(message("LAST", content_type="Image", media_url="x"))
        with patch.object(lead_scoring, "safe_ai_get_doc", return_value=self.convo), \
             patch.object(lead_scoring, "safe_ai_get_all", return_value=list(reversed(rows))) as read, \
             patch.object(lead_scoring, "get_conversation_policy", return_value=self.bundle), \
             patch.object(lead_scoring, "resolve_language_from_history", return_value={"label": "English"}):
            result = lead_scoring.recompute_conversation_metrics("C")
        self.assertEqual(result.lead_temperature, "Hot")
        self.assertEqual(result.details["history_count"], 46)
        self.assertEqual(result.details["policy_version"], 6)
        self.assertIn("scoring_media_text", read.call_args.kwargs["fields"])

    def test_history_limit_selects_recent_messages(self):
        self.bundle.sections["lead_scoring_policy"]["history_limit"] = 1
        with patch.object(lead_scoring, "safe_ai_get_doc", return_value=self.convo), \
             patch.object(lead_scoring, "safe_ai_get_all", return_value=[message()]) as read, \
             patch.object(lead_scoring, "get_conversation_policy", return_value=self.bundle), \
             patch.object(lead_scoring, "resolve_language_from_history", return_value={"label": "English"}):
            lead_scoring.recompute_conversation_metrics("C")
        self.assertEqual(read.call_args.kwargs["limit_page_length"], 1)
        self.assertEqual(read.call_args.kwargs["order_by"], "creation desc, name desc")

    def test_disabled_policy_never_overwrites_crm(self):
        result = lead_scoring.ScoreResult(0, "Cold", "", "policy_disabled")
        with patch.object(frappe, "db", MagicMock()), patch.object(lead_scoring, "assert_ai_doctype_permission"), \
             patch.object(lead_scoring, "recompute_conversation_metrics", return_value=result), \
             patch.object(lead_scoring, "sync_to_linked_lead") as sync:
            self.assertEqual(lead_scoring.score_and_sync_conversation("C"), {"source": "policy_disabled"})
        sync.assert_not_called()

    def test_score_and_audit_written_in_same_transaction(self):
        db = MagicMock()
        result = lead_scoring.ScoreResult(95, "Hot", "English", "policy", {"policy_version": 6})
        with patch.object(frappe, "db", db), patch.object(lead_scoring, "assert_ai_doctype_permission"), \
             patch.object(frappe.utils, "now_datetime", return_value="2026-11-18 16:00:00"), \
             patch.object(lead_scoring, "recompute_conversation_metrics", return_value=result), \
             patch.object(lead_scoring, "sync_to_linked_lead") as crm, \
             patch.object(lead_scoring, "sync_to_conversation") as convo:
            lead_scoring.score_and_sync_conversation("C")
        crm.assert_called_once_with("C", result); convo.assert_called_once_with("C", result)
        details = json.loads(db.set_value.call_args.args[3])
        self.assertEqual(details["policy_version"], 6)
        self.assertIn("scored_at", details)
        db.commit.assert_not_called()

    def test_link_sync_error_does_not_write_conversation_or_audit(self):
        db = MagicMock()
        with patch.object(frappe, "db", db), patch.object(lead_scoring, "assert_ai_doctype_permission"), \
             patch.object(lead_scoring, "recompute_conversation_metrics", return_value=lead_scoring.ScoreResult(80, "Hot", "", "policy")), \
             patch.object(lead_scoring, "sync_to_linked_lead", side_effect=frappe.QueryTimeoutError), \
             patch.object(lead_scoring, "sync_to_conversation") as sync:
            with self.assertRaises(frappe.QueryTimeoutError):
                lead_scoring.score_and_sync_conversation("C")
        sync.assert_not_called(); db.set_value.assert_not_called()

    def test_link_already_changed_in_memory_is_persisted_and_scheduled(self):
        doc = SimpleNamespace(name="C", linked_crm_lead="NEW", linked_reference_name="NEW", linked_reference_doctype="CRM Lead")
        updates = {"linked_crm_lead": "NEW", "linked_reference_name": "NEW", "linked_reference_doctype": "CRM Lead"}
        with patch.object(services, "safe_ai_get_value", return_value={}), \
             patch.object(services, "safe_ai_set_value") as write, \
             patch.object(services, "with_db_lock_retry", side_effect=lambda _, action: action()), \
             patch.object(services, "_enqueue_lead_scoring") as schedule:
            services._set_conversation_fields(doc, updates)
        self.assertEqual(write.call_args.args[2], updates)
        schedule.assert_called_once_with("C")

    def test_each_event_gets_job_even_while_previous_job_is_running(self):
        with patch.object(frappe, "enqueue") as enqueue:
            lead_ai._schedule("C"); lead_ai._schedule("C")
        first, second = [c.kwargs for c in enqueue.call_args_list]
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertTrue(first["enqueue_after_commit"])

    def test_manual_link_change_schedules_but_unchanged_document_does_not(self):
        doc = MagicMock(name="C")
        doc.has_value_changed.side_effect = lambda field: field == "linked_crm_lead"
        with patch.object(lead_ai, "_schedule") as schedule:
            lead_ai.on_conversation_update(doc)
            schedule.assert_called_once_with(doc.name)
        doc.has_value_changed.side_effect = lambda _: False
        with patch.object(lead_ai, "_schedule") as schedule:
            lead_ai.on_conversation_update(doc)
            schedule.assert_not_called()

    def test_media_completion_stores_evidence_and_schedules_after_commit(self):
        db = MagicMock()
        with patch.object(frappe, "db", db), patch.object(lead_scoring, "assert_ai_doctype_permission"), \
             patch.object(lead_scoring, "safe_ai_get_doc", return_value=SimpleNamespace(conversation="C")), \
             patch.object(lead_ai, "_schedule") as schedule:
            lead_scoring.record_media_scoring_text("M", "OCR evidence")
        self.assertEqual(db.set_value.call_args.args[2:4], ("scoring_media_text", "OCR evidence"))
        schedule.assert_called_once_with("C")

    def test_backfill_defaults_to_preview_and_requires_bounded_explicit_ids(self):
        with patch.object(lead_scoring, "preview_conversation_score", return_value={"score": 95}), \
             patch.object(lead_ai, "_schedule") as schedule:
            self.assertEqual(lead_scoring.rescore_batch(["C"]), [{"score": 95}])
            schedule.assert_not_called()
        for ids in [[], ["C"] * 101, "C"]:
            with self.assertRaises(ValueError):
                lead_scoring.rescore_batch(ids)
