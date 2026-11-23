import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from wa_chat_hub.maintenance import manual_batches as batches
from wa_chat_hub.maintenance import history_backfill as history
from wa_chat_hub import migrate
from wa_chat_hub.messaging import windows


RETIRED_PATCHES = (
    "v1_0.set_messaging_windows_from_history",
    "v1_0.backfill_crm_lead_meta_from_conversations",
    "v1_1.backfill_channel_account_medical_departments",
    "v1_1.migrate_conversation_crm_lead_links",
    "v1_1.backfill_conversation_identities",
    "v1_1.enqueue_indexed_phone_backfill",
    "v1_1.enqueue_messaging_window_backfill",
)


class AutomaticHistoryTests(unittest.TestCase):
    def test_pending_old_patches_neither_touch_history_nor_enqueue(self):
        with patch.object(frappe, "db", new=MagicMock()) as db, patch.object(frappe, "enqueue") as enqueue:
            for name in RETIRED_PATCHES:
                self.assertEqual(importlib.import_module("wa_chat_hub.patches." + name).execute(),
                                 {"status": "manual_backfill_required"})
            self.assertFalse(db.mock_calls)
            enqueue.assert_not_called()

    def test_stale_worker_entry_points_are_inert_even_with_force(self):
        with patch.object(frappe, "db", new=MagicMock()) as db, patch.object(frappe, "enqueue") as enqueue:
            for fn, kwargs in ((migrate.backfill_messaging_windows, {}),
                               (windows.backfill_messaging_windows_from_history, {}),
                               (windows.backfill_messaging_windows_from_history, {"force": True})):
                self.assertEqual(fn(**kwargs)["status"], "manual_backfill_required")
            self.assertFalse(db.mock_calls)
            enqueue.assert_not_called()

    def test_after_migrate_permissions_precede_workspace_without_backfill(self):
        calls = []
        with patch("wa_chat_hub.security.ensure_default_ai_doctype_permissions", side_effect=lambda: calls.append("permissions")), \
             patch("wa_chat_hub.setup_workspace.run", side_effect=lambda: calls.append("workspace")), \
             patch.object(migrate, "ensure_app_update_setting"), \
             patch.object(migrate, "backfill_messaging_windows") as backfill:
            migrate.after_migrate()
        self.assertEqual(calls, ["permissions", "workspace"])
        backfill.assert_not_called()

    def test_legacy_sql_helpers_default_to_preview(self):
        from wa_chat_hub.setup_agents import backfill_conversation_identities
        from wa_chat_hub.messaging.crm_lead_meta import backfill_all_linked_leads
        with patch.object(history, "run") as run:
            migrate.backfill_channel_account_medical_departments()
            migrate.migrate_conversation_crm_lead_links()
            backfill_conversation_identities()
            backfill_all_linked_leads()
        self.assertEqual(run.call_count, 4)
        self.assertTrue(run.call_args.kwargs["dry_run"])

    def test_permission_initialization_preserves_explicit_denials(self):
        from wa_chat_hub import security
        f = MagicMock()
        settings = MagicMock()
        denied = frappe._dict(doctype_name="Chat Conversation", is_active=0, allow_read=0, allow_write=0)
        settings.get.return_value = [denied, frappe._dict(doctype_name="WA Chat Hub Settings", is_active=0)]
        f.get_single.return_value = settings
        f.db.exists.side_effect = lambda dt, name: name in ("WA Chat Hub Settings", "Chat Conversation")
        with patch.object(security, "frappe", f):
            self.assertEqual(security.ensure_default_ai_doctype_permissions(), 0)
        settings.append.assert_not_called()
        settings.save.assert_not_called()
        self.assertEqual(denied.is_active, 0)
        self.assertEqual(denied.allow_read, 0)

    def test_manual_entry_is_not_http_whitelisted(self):
        self.assertNotIn(history.run, frappe.whitelisted)


class ManualBatchTests(unittest.TestCase):
    def setUp(self):
        self.f = MagicMock()
        self.f.flags = SimpleNamespace(in_migrate=False, in_install=False)
        self.f.local = SimpleNamespace(site="unit.test")
        self.f.db.exists.return_value = True
        self.f.db.sql.return_value = [(1,)]
        self.f.get_all.return_value = ["A", "B", "C"]
        self.callback = MagicMock()
        p = patch.object(batches, "frappe", self.f)
        p.start(); self.addCleanup(p.stop)

    def run_batch(self, **kwargs):
        return batches.run_batch(operation="test", doctype="Chat Conversation",
                                 process_row=self.callback, **kwargs)

    def test_preview_never_writes_or_advances(self):
        result = self.run_batch(batch_size=2)
        self.assertEqual(result["selected"], 2)
        self.assertEqual(result["last_name"], "")
        self.assertTrue(result["has_more"])
        self.callback.assert_not_called()
        self.f.db.sql.assert_not_called()
        self.f.db.commit.assert_not_called()

    def test_apply_is_bounded_and_returns_resume_cursor(self):
        result = self.run_batch(batch_size=2, dry_run=False, after_name="0")
        self.assertEqual([c.args[0] for c in self.callback.call_args_list], ["A", "B"])
        self.assertEqual(result["last_name"], "B")
        self.assertFalse(result["done"])
        self.assertEqual(self.f.get_all.call_args.kwargs["filters"]["name"], [">", "0"])
        self.assertEqual(self.f.get_all.call_args.kwargs["limit_page_length"], 3)
        self.f.db.commit.assert_called_once()
        self.assertIn("RELEASE_LOCK", self.f.db.sql.call_args.args[0])

    def test_failure_rolls_back_batch_without_committing_or_advancing(self):
        self.callback.side_effect = [None, RuntimeError("failed row")]
        with self.assertRaisesRegex(RuntimeError, "failed row"):
            self.run_batch(batch_size=2, dry_run=False)
        self.f.db.rollback.assert_called_once()
        self.f.db.commit.assert_not_called()
        self.assertIn("RELEASE_LOCK", self.f.db.sql.call_args.args[0])

    def test_busy_runner_does_not_read_or_write_history(self):
        self.f.db.sql.return_value = [(0,)]
        result = self.run_batch(dry_run=False, after_name="saved")
        self.assertEqual(result["status"], "busy")
        self.assertEqual(result["last_name"], "saved")
        self.f.get_all.assert_not_called()
        self.callback.assert_not_called()

    def test_missing_schema_and_invalid_arguments_fail_before_processing(self):
        for kwargs in ({"batch_size": 0}, {"batch_size": 501}, {"batch_size": True},
                       {"dry_run": "false"}, {"after_name": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_batch(**kwargs)
        self.f.db.exists.return_value = False
        with self.assertRaises(ValueError):
            self.run_batch()
        self.callback.assert_not_called()

    def test_migration_and_install_cannot_invoke_manual_backfill(self):
        for flag in ("in_migrate", "in_install"):
            setattr(self.f.flags, flag, True)
            with self.assertRaises(RuntimeError):
                self.run_batch(dry_run=False)
            setattr(self.f.flags, flag, False)
        self.f.get_all.assert_not_called()
        self.f.db.sql.assert_not_called()

    def test_empty_last_batch_finishes_without_resetting_cursor(self):
        self.f.get_all.return_value = []
        result = self.run_batch(dry_run=False, after_name="last")
        self.assertTrue(result["done"])
        self.assertEqual(result["last_name"], "last")
        self.callback.assert_not_called()


class BoundedSQLTests(unittest.TestCase):
    def test_identity_updates_all_have_selected_name_filter(self):
        from wa_chat_hub import setup_agents
        f = MagicMock()
        with patch.object(setup_agents, "frappe", f):
            setup_agents.backfill_conversation_identities(names=("selected",))
        self.assertEqual(f.db.sql.call_count, 4)
        for call in f.db.sql.call_args_list:
            self.assertIn("name IN %(names)s", call.args[0])
            self.assertEqual(call.args[1], {"names": ("selected",)})

    def test_link_and_department_updates_are_bounded(self):
        f = MagicMock()
        with patch.object(migrate, "frappe", f):
            migrate.migrate_conversation_crm_lead_links(names=("selected",))
            migrate.backfill_channel_account_medical_departments(names=("selected",))
        self.assertEqual(f.db.sql.call_count, 3)
        for call in f.db.sql.call_args_list:
            self.assertIn("name IN %(names)s", call.args[0])
            self.assertEqual(call.args[1], {"names": ("selected",)})
