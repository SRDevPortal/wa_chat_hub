"""Opt-in local DB checks for attachment lookups and File deletion protection.

Run from the bench sites directory with WA_ATTACHMENT_TEST_SITE=localhost.
Test rows bypass message hooks and are rolled back; no files or messages are sent.
The attachment-index migration must already have run on this local test site.
"""
import os
import secrets
import unittest
import uuid
from unittest.mock import patch

import frappe
from frappe.model.delete_doc import check_if_doc_is_linked

from wa_chat_hub.maintenance.attachment_indexes import _equivalent_index, _indexes


@unittest.skipUnless(os.environ.get("WA_ATTACHMENT_TEST_SITE"), "requires explicit local test site")
class AttachmentLookupDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        site = os.environ["WA_ATTACHMENT_TEST_SITE"]
        if site != "localhost":
            raise RuntimeError("Attachment DB regressions are restricted to localhost")
        frappe.init(site=site, sites_path=os.path.abspath("."))
        frappe.connect()
        frappe.set_user("Administrator")
        if not _equivalent_index(_indexes()):
            frappe.destroy()
            raise RuntimeError("Run the attachment index migration on localhost first")

    @classmethod
    def tearDownClass(cls):
        frappe.db.rollback()
        frappe.destroy()

    def setUp(self):
        self.addCleanup(frappe.db.rollback)
        self.file_name = "attachment-regression-" + uuid.uuid4().hex
        self.ids = [-secrets.randbelow(10**17) - 1 for _ in range(3)]
        for name, attachment, modified in [
            (self.ids[0], self.file_name, "2026-01-01 10:00:00"),
            (self.ids[1], self.file_name, "2026-01-01 11:00:00"),
            (self.ids[2], self.file_name + "-other", "2026-01-01 12:00:00"),
        ]:
            frappe.db.sql(
                "INSERT INTO `tabChat Message` "
                "(name, attachment_file, modified, docstatus) VALUES (%s,%s,%s,0)",
                (name, attachment, modified),
            )

    def test_latest_attachment_lookup_returns_only_matching_file(self):
        rows = frappe.get_all(
            "Chat Message", filters={"attachment_file": self.file_name},
            fields=["name"], order_by="modified desc", limit_page_length=1,
        )
        self.assertEqual(str(rows[0].name), str(self.ids[1]))

    def test_attachment_lookup_uses_index_without_sorting_all_messages(self):
        plan = frappe.db.sql(
            "EXPLAIN SELECT name FROM `tabChat Message` "
            "WHERE attachment_file=%s ORDER BY modified DESC LIMIT 1",
            (self.file_name,), as_dict=True,
        )[0]
        self.assertEqual(plan.type, "ref")
        self.assertTrue(plan.key)
        self.assertNotIn("filesort", (plan.Extra or "").lower())

    def test_linked_file_deletion_still_refused(self):
        doc = frappe.get_doc({"doctype": "File", "name": self.file_name})
        links = [{"parent": "Chat Message", "fieldname": "attachment_file", "issingle": 0}]
        with patch("frappe.model.rename_doc.get_link_fields", return_value=links):
            with self.assertRaises(frappe.LinkExistsError):
                check_if_doc_is_linked(doc)

    def test_unlinked_file_passes_same_link_check(self):
        doc = frappe.get_doc({"doctype": "File", "name": self.file_name + "-unused"})
        links = [{"parent": "Chat Message", "fieldname": "attachment_file", "issingle": 0}]
        with patch("frappe.model.rename_doc.get_link_fields", return_value=links):
            check_if_doc_is_linked(doc)
