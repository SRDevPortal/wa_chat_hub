from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from wa_chat_hub.ai import lead_notes, lead_scoring, media_transcription
from wa_chat_hub.security import WAChatHubSecurityError


class DerivedLeadWriteTests(TestCase):
    def test_notes_use_locked_current_value_without_full_lead_save(self):
        db = MagicMock()
        db.sql.return_value = [("concurrent newer note",)]
        doc = MagicMock(attachment_tag=None)
        doc.save.side_effect = frappe.MandatoryError("attachment_tag")
        with patch.object(frappe, "db", db), patch.object(lead_notes, "assert_ai_doctype_permission"), \
             patch.object(lead_notes, "safe_ai_get_doc", return_value=doc), \
             patch.object(lead_notes, "safe_ai_set_value") as write:
            lead_notes.append_lead_note("CRM Lead", "L", "OCR note", "sr_lead_notes")
        self.assertIn("FOR UPDATE", db.sql.call_args.args[0])
        write.assert_called_once_with("CRM Lead", "L", {"sr_lead_notes": "concurrent newer note\n\nOCR note"})
        doc.save.assert_not_called()
        doc.add_comment.assert_called_once_with("Comment", "OCR note")
        db.commit.assert_not_called()
        db.rollback.assert_not_called()

    def test_permission_failure_happens_before_any_write(self):
        db = MagicMock()
        with patch.object(frappe, "db", db), \
             patch.object(lead_notes, "assert_ai_doctype_permission", side_effect=WAChatHubSecurityError), \
             patch.object(lead_notes, "safe_ai_set_value") as write:
            with self.assertRaises(WAChatHubSecurityError):
                lead_notes.append_lead_note("CRM Lead", "L", "note", "sr_lead_notes")
        db.sql.assert_not_called()
        write.assert_not_called()

    def test_trimming_keeps_newest_note_within_storage_limit(self):
        for old, note in [("a"*5999, "new block"), ("a"*6000, "b"*5990), ("old", "b"*7000)]:
            merged = lead_notes.merge_note(old, note)
            self.assertLessEqual(len(merged), 6000)
            if len(note) < 6000:
                self.assertTrue(merged.endswith(note))

    def test_media_uses_same_atomic_append(self):
        with patch.object(media_transcription, "_resolve_notes_fieldname", return_value="notes"), \
             patch.object(media_transcription, "append_lead_note") as append:
            media_transcription._append_to_lead_notes("CRM Lead", "L", "transcript")
        append.assert_called_once_with("CRM Lead", "L", "transcript", "notes")

    def test_scoring_contention_retries_whole_job_and_restores_session(self):
        for error in (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
            db = MagicMock()
            db.sql.return_value = [(50,)]
            with patch.object(frappe, "db", db), \
                 patch.object(lead_scoring, "score_and_sync_conversation", side_effect=error) as score:
                with self.assertRaises(frappe.RetryBackgroundJobError):
                    lead_scoring.score_and_sync_conversation_job("C")
            score.assert_called_once_with("C")
            self.assertEqual(db.sql.call_args.args, ("SET SESSION innodb_lock_wait_timeout=%s", (50,)))
            db.commit.assert_not_called()

    def test_scoring_does_not_retry_unrelated_errors(self):
        db = MagicMock()
        db.sql.return_value = [(50,)]
        with patch.object(frappe, "db", db), \
             patch.object(lead_scoring, "score_and_sync_conversation", side_effect=ValueError("bad policy")):
            with self.assertRaisesRegex(ValueError, "bad policy"):
                lead_scoring.score_and_sync_conversation_job("C")
