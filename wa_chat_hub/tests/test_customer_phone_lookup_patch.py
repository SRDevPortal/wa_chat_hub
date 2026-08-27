from __future__ import annotations

from unittest import TestCase
from unittest.mock import call, patch

from wa_chat_hub.patches.v1_0.add_customer_indexed_phone_lookup import (
    CUSTOMER_PHONE_FIELDS,
    ensure_customer_phone_lookup_schema,
)


PATCH_MODULE = "wa_chat_hub.patches.v1_0.add_customer_indexed_phone_lookup"


class TestCustomerPhoneLookupPatch(TestCase):
    @patch(f"{PATCH_MODULE}.create_custom_fields")
    @patch(f"{PATCH_MODULE}.frappe")
    def test_creates_customer_fields_and_composite_indexes(
        self,
        frappe,
        create_fields,
    ):
        frappe.db.exists.return_value = True
        frappe.db.has_column.return_value = True

        ensure_customer_phone_lookup_schema()

        create_fields.assert_called_once_with(
            {"Customer": [dict(field) for field in CUSTOMER_PHONE_FIELDS]},
            update=True,
            ignore_validate=True,
        )
        self.assertEqual(
            frappe.db.add_index.call_args_list,
            [
                call(
                    "Customer",
                    ["vobiz_normalized_phone", "modified"],
                    index_name="idx_wa_customer_normalized_modified",
                ),
                call(
                    "Customer",
                    ["vobiz_mobile_last10", "modified"],
                    index_name="idx_wa_customer_mobile_last10_modified",
                ),
            ],
        )

    @patch(f"{PATCH_MODULE}.create_custom_fields")
    @patch(f"{PATCH_MODULE}.frappe")
    def test_skips_indexes_when_columns_are_unavailable(
        self,
        frappe,
        _create_fields,
    ):
        frappe.db.exists.return_value = True
        frappe.db.has_column.return_value = False

        ensure_customer_phone_lookup_schema()

        frappe.db.add_index.assert_not_called()

    @patch(f"{PATCH_MODULE}.create_custom_fields")
    @patch(f"{PATCH_MODULE}.frappe")
    def test_does_nothing_without_customer_doctype(self, frappe, create_fields):
        frappe.db.exists.return_value = False

        ensure_customer_phone_lookup_schema()

        create_fields.assert_not_called()
