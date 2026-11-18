import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import frappe as real_frappe
from wa_chat_hub.maintenance import phone_backfill as mod


class Timeout(Exception): pass
class Deadlock(Exception): pass


class ManualBackfillTests(unittest.TestCase):
    def setUp(self):
        self.f = MagicMock()
        self.f.local = SimpleNamespace(site="test.local")
        self.f.scrub.side_effect = lambda s: s.lower().replace(" ", "_")
        self.f.QueryTimeoutError = Timeout
        self.f.QueryDeadlockError = Deadlock
        self.f.db.get_global.return_value = ""
        self.f.db.exists.return_value = True
        self.acquired = True
        self.latest = real_frappe._dict(name="A", mobile_no="9876543210", vobiz_mobile_last10="")
        self.f.get_all.return_value = [self.latest.copy()]
        self.row_failure = None
        def sql(q, values=None, **kwargs):
            if "GET_LOCK" in q: return [(int(self.acquired),)]
            if "@@SESSION.innodb_lock_wait_timeout" in q: return [(50,)]
            if "FOR UPDATE" in q:
                if self.row_failure: raise self.row_failure
                return [self.latest]
            return [(1,)]
        self.f.db.sql.side_effect = sql
        p=patch.object(mod, "frappe", self.f);p.start();self.addCleanup(p.stop)
        p=patch.object(mod, "_existing_fields", side_effect=lambda dt, fields:
                       ["mobile_no"] if fields is mod.SOURCE_FIELDS else ["vobiz_mobile_last10"])
        p.start();self.addCleanup(p.stop)

    def test_dry_run_does_not_write_or_advance(self):
        result=mod.backfill_phone_keys(dry_run=True)
        self.assertEqual(result["updated"], 1)
        self.f.db.set_value.assert_not_called(); self.f.db.set_global.assert_not_called()
        self.f.db.commit.assert_not_called()

    def test_reads_latest_source_under_lock(self):
        self.latest.mobile_no="9123456780"
        result=mod.backfill_phone_keys()
        self.assertEqual(result["updated"], 1)
        self.f.db.set_value.assert_called_once_with("CRM Lead", "A", {"vobiz_mobile_last10":"9123456780"}, update_modified=False)
        self.f.db.set_global.assert_called_once_with(mod._progress_key("CRM Lead"), "A")
        self.f.db.commit.assert_called_once()

    def test_lock_conflict_preserves_checkpoint_and_does_not_skip(self):
        self.row_failure=Timeout()
        result=mod.backfill_phone_keys()
        self.assertEqual(result["status"], "retry")
        self.assertEqual(result["last_name"], "")
        self.assertFalse(result["done"])
        self.f.db.rollback.assert_called_once();self.f.db.set_global.assert_not_called()
        self.assertEqual(result["updated"], 0)
        self.assertIn(unittest.mock.call("SET SESSION innodb_lock_wait_timeout=%s", (50,)),self.f.db.sql.call_args_list)

    def test_busy_runner_and_reset_do_not_mutate(self):
        self.acquired=False
        self.assertEqual(mod.backfill_phone_keys()["status"], "busy")
        with self.assertRaises(RuntimeError): mod.reset_phone_backfill()
        self.f.db.set_global.assert_not_called();self.f.db.set_value.assert_not_called()

    def test_old_background_job_cannot_activate_or_enqueue(self):
        for i in [0,4,999]:
            self.assertEqual(mod.run_indexed_phone_backfill(i)["status"], "manual_backfill_required")
        self.f.db.set_single_value.assert_not_called();self.f.enqueue.assert_not_called()

    def test_missing_schema_is_not_completion(self):
        self.f.db.exists.return_value=False
        with self.assertRaises(ValueError):mod.backfill_phone_keys()

    def test_verification_classifies_missing_mismatch_invalid_and_empty(self):
        self.f.get_all.return_value=[real_frappe._dict(name=str(i),mobile_no=raw,vobiz_mobile_last10=stored)
          for i,(raw,stored) in enumerate([("9876543210",""),("9876543210","1111111111"),("00","00"),("","")])]
        r=mod.verify_phone_keys()
        self.assertEqual(r["counts"]["vobiz_mobile_last10"],dict(missing=1,mismatched=1,invalid_source=1,not_applicable=1))
        self.f.db.set_value.assert_not_called()

    def test_reset_returns_old_checkpoint(self):
        self.f.db.get_global.return_value="old"
        self.assertEqual(mod.reset_phone_backfill()["previous_last_name"],"old")

    def test_mutation_commands_are_not_http_whitelisted(self):
        for fn in [mod.backfill_phone_keys,mod.reset_phone_backfill]:
            self.assertNotIn(fn, real_frappe.whitelisted)


if __name__ == "__main__":unittest.main()
