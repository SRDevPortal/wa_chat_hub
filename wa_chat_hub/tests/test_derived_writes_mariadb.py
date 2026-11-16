"""Exercise note/score writes on an isolated socket-only MariaDB, never a site."""
from contextlib import ExitStack
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from unittest import TestCase, skipUnless
from unittest.mock import MagicMock, patch

import frappe
import pymysql
from wa_chat_hub.ai import lead_notes, lead_scoring


class Database:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = None

    def sql(self, query, values=None):
        with self.connection.cursor() as cursor:
            cursor.execute(query, values)
            self.rowcount = cursor.rowcount
            return cursor.fetchall()

    def set_value(self, doctype, name, values):
        columns = ", ".join("`" + field + "`=%s" for field in values)
        self.sql("UPDATE `tab" + doctype + "` SET " + columns + " WHERE name=%s", (*values.values(), name))


@skipUnless(shutil.which("mariadbd") and shutil.which("mariadb-install-db"), "requires disposable MariaDB tooling")
class DerivedWriteDatabaseTests(TestCase):
    @classmethod
    def setUpClass(cls):
        folder = tempfile.TemporaryDirectory(prefix="wa-derived-test-")
        cls.addClassCleanup(folder.cleanup)
        root = Path(folder.name)
        output = (root / "bootstrap.log").open("w")
        cls.addClassCleanup(output.close)
        subprocess.run(["mariadb-install-db", "--no-defaults", "--datadir=" + str(root / "data"),
                        "--auth-root-authentication-method=normal", "--skip-test-db"],
                       check=True, stdout=output, stderr=subprocess.STDOUT, timeout=45)
        cls.socket = str(root / "db.sock")
        server = subprocess.Popen(["mariadbd", "--no-defaults", "--datadir=" + str(root / "data"),
            "--socket=" + cls.socket, "--pid-file=" + str(root / "db.pid"), "--skip-networking",
            "--skip-log-bin", "--innodb-buffer-pool-size=64M", "--max-connections=10",
            "--log-error=" + str(root / "server.log")], stdout=output, stderr=subprocess.STDOUT)
        def stop():
            server.terminate()
            server.wait(timeout=20)
        cls.addClassCleanup(stop)
        for _ in range(150):
            try:
                admin = pymysql.connect(unix_socket=cls.socket, user="root", autocommit=True)
                break
            except pymysql.err.OperationalError:
                if server.poll() is not None:
                    raise RuntimeError((root / "server.log").read_text())
                time.sleep(.1)
        else:
            raise RuntimeError("Disposable MariaDB did not start")
        with admin:
            with admin.cursor() as cursor:
                cursor.execute("CREATE DATABASE derived_test")
        cls.a = pymysql.connect(unix_socket=cls.socket, user="root", database="derived_test", autocommit=False)
        cls.b = pymysql.connect(unix_socket=cls.socket, user="root", database="derived_test", autocommit=False)
        cls.addClassCleanup(cls.a.close)
        cls.addClassCleanup(cls.b.close)
        cls.db = Database(cls.a)
        cls.db.sql("CREATE TABLE `tabCRM Lead` (name varchar(140) PRIMARY KEY, notes TEXT, attachment_tag TEXT, "
                   "lead_score DOUBLE, lead_lan varchar(40), lead_temperature varchar(40)) ENGINE=InnoDB")
        cls.a.commit()

    def setUp(self):
        self.a.rollback()
        self.b.rollback()
        self.db.sql("DELETE FROM `tabCRM Lead`")
        self.db.sql("INSERT INTO `tabCRM Lead` VALUES ('L','existing',NULL,10,'English','Cold')")
        self.a.commit()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(lead_notes, "assert_ai_doctype_permission"))
        self.stack.enter_context(patch.object(lead_notes, "safe_ai_set_value", side_effect=lambda *a: frappe.db.set_value(*a)))
        self.doc = MagicMock(attachment_tag=None)
        self.doc.save.side_effect = frappe.MandatoryError("attachment_tag")
        self.stack.enter_context(patch.object(lead_notes, "safe_ai_get_doc", return_value=self.doc))

    def test_concurrent_appends_preserve_both_notes_despite_old_read_snapshot(self):
        other = Database(self.b)
        other.sql("SELECT notes FROM `tabCRM Lead` WHERE name='L'")
        with patch.object(frappe, "db", self.db):
            lead_notes.append_lead_note("CRM Lead", "L", "first", "notes")
        entered, finished = threading.Event(), threading.Event()
        errors = []
        def append_second():
            entered.set()
            try:
                lead_notes.append_lead_note("CRM Lead", "L", "second", "notes")
                self.b.commit()
            except Exception as exc:
                errors.append(exc)
                self.b.rollback()
            finally:
                finished.set()
        with patch.object(frappe, "db", other):
            worker = threading.Thread(target=append_second)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertFalse(finished.wait(.1))
            finally:
                self.a.commit()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.db.sql("SELECT notes,attachment_tag FROM `tabCRM Lead` WHERE name='L'")[0],
                         ("existing\n\nfirst\n\nsecond", None))
        self.doc.save.assert_not_called()
        self.assertEqual(self.doc.add_comment.call_count, 2)

    def test_note_write_rolls_back_with_failed_comment(self):
        self.doc.add_comment.side_effect = ValueError("comment failed")
        with patch.object(frappe, "db", self.db):
            with self.assertRaisesRegex(ValueError, "comment failed"):
                lead_notes.append_lead_note("CRM Lead", "L", "new", "notes")
        self.a.rollback()
        self.assertEqual(self.db.sql("SELECT notes FROM `tabCRM Lead` WHERE name='L'")[0][0], "existing")

    def test_scoring_only_changes_derived_fields_and_skips_identical_values(self):
        with patch.object(frappe, "db", self.db), \
             patch.object(lead_scoring, "assert_ai_doctype_permission"), \
             patch.object(lead_scoring, "safe_ai_get_doc"), \
             patch.object(lead_scoring, "safe_ai_exists", return_value=True), \
             patch.object(lead_scoring, "get_conversation_crm_lead", return_value="L"), \
             patch.object(frappe, "get_meta", return_value=MagicMock()):
            lead_scoring.sync_to_linked_lead("C", lead_scoring.ScoreResult(10, "Cold", "English", "test"))
            self.assertEqual(self.db.rowcount, 0)
            lead_scoring.sync_to_linked_lead("C", lead_scoring.ScoreResult(50, "Warm", "Hindi", "test"))
            self.assertEqual(self.db.rowcount, 1)
        self.assertEqual(self.db.sql("SELECT lead_score,lead_temperature,lead_lan,notes,attachment_tag FROM `tabCRM Lead`")[0],
                         (50, "Warm", "Hindi", "existing", None))
        self.a.rollback()
