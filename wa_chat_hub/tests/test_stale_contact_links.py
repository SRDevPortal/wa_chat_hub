from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from wa_chat_hub.services import _delete_stale_contact_lead_links_for_phone


class TestStaleContactLeadLinks(TestCase):
    def test_deletes_only_missing_contact_lead_links_for_phone(self):
        frappe_mock = MagicMock()
        frappe_mock.db.sql.return_value = [
            frappe._dict(name="DL-MISSING", link_doctype="Lead", link_name="CRM-LEAD-MISSING"),
            frappe._dict(name="DL-EXISTS", link_doctype="Lead", link_name="CRM-LEAD-EXISTS"),
        ]
        frappe_mock.db.exists.side_effect = [None, "CRM-LEAD-EXISTS"]

        with patch("wa_chat_hub.services.frappe", frappe_mock):
            deleted = _delete_stale_contact_lead_links_for_phone("919084553059")

        self.assertEqual(deleted, 1)
        frappe_mock.delete_doc.assert_called_once_with(
            "Dynamic Link",
            "DL-MISSING",
            ignore_permissions=True,
            force=True,
        )

