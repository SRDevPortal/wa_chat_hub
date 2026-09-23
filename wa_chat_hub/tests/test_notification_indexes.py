from unittest import TestCase
from unittest.mock import MagicMock, patch

from wa_chat_hub.maintenance import notification_indexes as indexes


def index_rows(name=None, fields=None, **overrides):
    return [
        dict(INDEX_NAME=name or indexes.INDEX_NAME, COLUMN_NAME=field,
             SEQ_IN_INDEX=position, SUB_PART=None, INDEX_TYPE="BTREE", IGNORED="NO", **overrides)
        for position, field in enumerate(fields or indexes.FIELDS, 1)
    ]


class TestNotificationIndexes(TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.table_exists.return_value = True
        self.db.has_column.return_value = True
        self.catalog = []
        self.statements = []

        def sql(statement, values=None, **kwargs):
            self.statements.append((statement, values))
            if "information_schema.STATISTICS" in statement:
                return self.catalog
            if statement == "SELECT @@SESSION.lock_wait_timeout":
                return [(123,)]
            return []

        def ddl(statement):
            self.catalog = index_rows()

        self.db.sql.side_effect = sql
        self.db.sql_ddl.side_effect = ddl
        self.patcher = patch.object(indexes.frappe, "db", self.db, create=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_online_build_and_repeated_execution(self):
        self.assertEqual(indexes.ensure_notification_event_index()["status"], "created")
        statement = self.db.sql_ddl.call_args.args[0]
        self.assertIn("ALGORITHM=INPLACE, LOCK=NONE", statement)
        self.assertEqual(self.statements[-2], ("SET SESSION lock_wait_timeout = %s", (123,)))
        self.assertEqual(indexes.ensure_notification_event_index()["status"], "already_present")
        self.db.sql_ddl.assert_called_once()

    def test_equivalent_longer_index_under_another_name_is_reused(self):
        self.catalog = index_rows("existing_lookup", (*indexes.FIELDS, "name"))
        self.assertEqual(indexes.ensure_notification_event_index()["index"], "existing_lookup")
        self.db.sql_ddl.assert_not_called()

    def test_partial_column_index_is_not_equivalent(self):
        self.catalog = index_rows("prefix_lookup")
        self.catalog[0]["SUB_PART"] = 20
        self.assertEqual(indexes.ensure_notification_event_index()["status"], "created")

    def test_ignored_index_is_not_equivalent(self):
        self.catalog = index_rows("ignored_lookup")
        for row in self.catalog:
            row["IGNORED"] = "YES"
        self.assertEqual(indexes.ensure_notification_event_index()["status"], "created")

    def test_wrong_column_order_is_not_equivalent(self):
        self.catalog = index_rows("wrong_order", tuple(reversed(indexes.FIELDS)))
        self.assertEqual(indexes.ensure_notification_event_index()["status"], "created")

    def test_conflicting_name_fails_without_dropping_an_index(self):
        self.catalog = index_rows(fields=("modified",))
        with self.assertRaisesRegex(RuntimeError, "unexpected definition"):
            indexes.ensure_notification_event_index()
        self.db.sql_ddl.assert_not_called()

    def test_online_failure_restores_wait_and_never_falls_back(self):
        self.db.sql_ddl.side_effect = RuntimeError("metadata lock timeout")
        with self.assertRaisesRegex(RuntimeError, "metadata lock timeout"):
            indexes.ensure_notification_event_index()
        self.assertEqual(self.statements[-1], ("SET SESSION lock_wait_timeout = %s", (123,)))
        self.db.sql_ddl.assert_called_once()

    def test_missing_fields_fail_without_ddl(self):
        self.db.has_column.return_value = False
        with self.assertRaisesRegex(RuntimeError, "fields are missing"):
            indexes.ensure_notification_event_index()
        self.db.sql_ddl.assert_not_called()

    def test_missing_table_is_safe_during_installation(self):
        self.db.table_exists.return_value = False
        self.assertEqual(indexes.ensure_notification_event_index()["status"], "table_absent")
        self.db.sql_ddl.assert_not_called()

    def test_failed_verification_is_not_reported_as_success(self):
        self.db.sql_ddl.side_effect = None
        with self.assertRaisesRegex(RuntimeError, "not found"):
            indexes.ensure_notification_event_index()
