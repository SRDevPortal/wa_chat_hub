from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.interakt import templates_api as api


class TestTemplateVariables(TestCase):
    def test_repeated_and_distinct_multilingual_parameters(self):
        for body, count in (
            ("Hello {{1}}, नमस्ते {{1}}, আবার {{1}}", 1),
            ("Hello {{1}}, appointment {{2}}", 2),
            ("{{2}} नमस्ते {{1}}, {{2}} again {{1}}", 2),
            ("{{3}} {{1}} {{2}} {{3}} {{1}}", 3),
        ):
            for metadata in (0, 9):
                with self.subTest(body=body, metadata=metadata):
                    row = api._normalize_templates([{
                        "name": "welcome", "body": body,
                        "body_variable_count": metadata,
                    }])[0]
                    self.assertEqual(row["body_variable_count"], count)
                    self.assertEqual([s["index"] for s in row["body_variables"]], list(range(1, count + 1)))
                    payload = {"template_name": "welcome", "body_values": [str(i) for i in range(count)]}
                    self.assertEqual(api.resolve_approved_template("account", payload, [row])["body_values"], payload["body_values"])

    def test_header_and_body_have_separate_parameters(self):
        self.assertEqual(api._count_variables(
            {"body_variable_count": 4}, "{{1}} {{1}}", "{{2}} {{1}} {{2}}"
        ), (1, 2))

    def test_missing_preview_preserves_metadata(self):
        row = api._normalize_templates([{"name": "welcome", "body_variable_count": 2}])[0]
        self.assertEqual(row["body_variable_count"], 2)
        self.assertEqual(len(row["body_variables"]), 2)
        self.assertEqual(api._count_variables({"variable_present": "Yes"}, ""), (1, 0))
        self.assertEqual(api._count_variables({}, "No variables"), (0, 0))

    def test_stale_cached_count_is_reconciled_at_send(self):
        row = {"name": "welcome", "body_preview": "{{1}} नमस्ते {{1}}", "body_variable_count": 2}
        payload = {"template_name": "welcome", "body_values": ["Test"]}
        self.assertEqual(api.resolve_approved_template("account", payload, [row]), payload)

    def test_missing_extra_and_empty_values_still_rejected(self):
        row = {"name": "welcome", "body_preview": "{{1}} {{2}} {{1}}", "body_variable_count": 3}
        for values in (["one"], ["one", "two", "three"], ["one", " "]):
            with self.subTest(values=values), patch.object(api.frappe, "throw", side_effect=frappe.ValidationError):
                with self.assertRaises(frappe.ValidationError):
                    api.resolve_approved_template("account", {"template_name": "welcome", "body_values": values}, [row])

    def test_manual_catalog_reconciles_count(self):
        account = frappe._dict(interakt_template_catalog=[frappe._dict(
            template_name="welcome", body_preview="{{1}} {{1}}", body_variable_count=2,
        )])
        row = api._load_manual_catalog(account)[0]
        self.assertEqual(row["body_variable_count"], 1)
        self.assertEqual(len(row["body_variables"]), 1)
