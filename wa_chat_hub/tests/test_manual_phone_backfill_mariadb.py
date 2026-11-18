"""Opt-in local MariaDB tests using isolated generated tables, never business rows."""
import os
import unittest
import uuid
from unittest.mock import patch
import frappe
from wa_chat_hub.maintenance import phone_backfill as mod


@unittest.skipUnless(os.environ.get("LOCAL_PHONE_TEST_SITE"), "requires explicit local test site")
class MariaDBBackfillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        site = os.environ["LOCAL_PHONE_TEST_SITE"]
        if not site.endswith((".localhost", ".local")):
            raise ValueError("Only explicitly named local sites are permitted")
        frappe.init(site=site)
        frappe.connect()
        frappe.set_user("Administrator")

    @classmethod
    def tearDownClass(cls):
        frappe.destroy()

    def setUp(self):
        self.table = "_phone_test_" + uuid.uuid4().hex
        self.progress = self.table + "_progress"
        self.sql = frappe.db.sql
        self.sql(f"CREATE TABLE `{self.table}` (name varchar(140) PRIMARY KEY, mobile_no varchar(140), vobiz_mobile_last10 varchar(140)) ENGINE=InnoDB")
        self.sql(f"CREATE TABLE `{self.progress}` (id int PRIMARY KEY, cursor_value varchar(140)) ENGINE=InnoDB")
        self.sql(f"INSERT INTO `{self.table}` VALUES ('A','9876543210',''),('B','9123456780','')")
        self.sql(f"INSERT INTO `{self.progress}` VALUES (1,'')")
        frappe.db.commit()
        self.other=frappe.db.get_connection()
        self.after_read=None
        def get_all(doctype, filters=None, **kwargs):
            args=(filters["name"][1],kwargs["limit_page_length"]) if filters else (kwargs["limit_page_length"],)
            where="WHERE name > %s" if filters else ""
            rows=self.sql(f"SELECT * FROM `{self.table}` {where} ORDER BY name LIMIT %s",args,as_dict=True)
            if self.after_read:
                callback=self.after_read; self.after_read=None; callback()
            return rows
        def mapped_sql(query, *args, **kwargs):
            return self.sql(query.replace("`tabCRM Lead`",f"`{self.table}`"),*args,**kwargs)
        def set_value(dt,name,values,**kwargs):
            self.sql(f"UPDATE `{self.table}` SET vobiz_mobile_last10=%s WHERE name=%s",(values["vobiz_mobile_last10"],name))
        self.patches=[
            patch.object(mod,"_schema",return_value=(["mobile_no"],["vobiz_mobile_last10"])),
            patch.object(frappe,"get_all",side_effect=get_all),
            patch.object(frappe.db,"sql",side_effect=mapped_sql),
            patch.object(frappe.db,"set_value",side_effect=set_value),
            patch.object(frappe.db,"get_global",side_effect=lambda key:self.sql(f"SELECT cursor_value FROM `{self.progress}`")[0][0]),
            patch.object(frappe.db,"set_global",side_effect=lambda key,value:self.sql(f"UPDATE `{self.progress}` SET cursor_value=%s",(value,))),
        ]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.other.rollback();self.other.close();frappe.db.rollback()
        self.sql(f"DROP TABLE `{self.table}`")
        self.sql(f"DROP TABLE `{self.progress}`")
        frappe.db.commit()

    def test_concurrent_source_edit_uses_current_locked_value(self):
        def change():
            with self.other.cursor() as cur:cur.execute(f"UPDATE `{self.table}` SET mobile_no='9998887776' WHERE name='A'")
            self.other.commit()
        self.after_read=change
        result=mod.backfill_phone_keys()
        self.assertTrue(result["done"])
        self.assertEqual(self.sql(f"SELECT vobiz_mobile_last10 FROM `{self.table}` WHERE name='A'")[0][0],"9998887776")

    def test_locked_second_row_rolls_back_first_row_and_checkpoint_then_resumes(self):
        with self.other.cursor() as cur:cur.execute(f"SELECT name FROM `{self.table}` WHERE name='B' FOR UPDATE")
        result=mod.backfill_phone_keys()
        self.assertEqual(result["status"],"retry")
        self.assertEqual(self.sql(f"SELECT vobiz_mobile_last10 FROM `{self.table}` WHERE name='A'")[0][0],"")
        self.assertEqual(self.sql(f"SELECT cursor_value FROM `{self.progress}`")[0][0],"")
        self.other.rollback();frappe.db.rollback()
        self.assertTrue(mod.backfill_phone_keys()["done"])
        self.assertEqual(self.sql(f"SELECT cursor_value FROM `{self.progress}`")[0][0],"B")

    def test_dry_run_leaves_database_and_checkpoint_unchanged(self):
        self.assertEqual(mod.backfill_phone_keys(dry_run=True)["updated"],2)
        self.assertEqual(self.sql(f"SELECT COUNT(*) FROM `{self.table}` WHERE vobiz_mobile_last10!=''")[0][0],0)
        self.assertEqual(self.sql(f"SELECT cursor_value FROM `{self.progress}`")[0][0],"")
