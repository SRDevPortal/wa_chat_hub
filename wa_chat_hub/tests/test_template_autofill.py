from unittest import TestCase
from unittest.mock import patch
import frappe
from wa_chat_hub.interakt.template_autofill import suggest_values, add_suggestions


class TestTemplateAutofill(TestCase):
    def test_known_values_and_unknown_date(self):
        row = {"body_preview": "Hello {{1}}, Department: {{2}}, appointment {{3}}. Agent name: {{4}}"}
        self.assertEqual(suggest_values(row, {"contact_name": "Amit", "department": "Support", "agent_name": "Sam"}), {"body_1": "Amit", "body_2": "Support", "body_4": "Sam"})

    def test_does_not_guess_from_position_or_template_name(self):
        self.assertEqual(suggest_values({"name": "patient_visit", "body_preview": "Your appointment is {{1}}"}, {"contact_name": "Amit"}), {})

    def test_phone_display_name_is_not_exposed(self):
        self.assertEqual(suggest_values({"body_preview": "Dear {{1}}"}, {"contact_name": "+91 98765 43210"}), {})

    def test_missing_values_are_blank(self):
        self.assertEqual(suggest_values({"body_preview": "Hello {{1}}"}, {}), {})

    def test_header_text_but_not_media(self):
        row = {"header_preview": "Hello {{1}}", "header_format": "TEXT"}
        self.assertEqual(suggest_values(row, {"contact_name": "Amit"}), {"header_1": "Amit"})
        row["header_format"] = "IMAGE"
        self.assertEqual(suggest_values(row, {"contact_name": "Amit"}), {})

    def test_context_does_not_leak_between_variables(self):
        self.assertEqual(suggest_values({"body_preview": "Hello {{1}}, your code is {{2}}"}, {"contact_name": "Amit"}), {"body_1": "Amit"})

    def test_does_not_mutate_shared_catalog(self):
        rows = [{"body_preview": "Hi {{1}}"}]
        with patch("wa_chat_hub.interakt.template_autofill.frappe.db.get_value", return_value="Agent"), patch("wa_chat_hub.interakt.template_autofill.frappe.get_doc", side_effect=[frappe._dict(contact="c"), frappe._dict(display_name="Amit")]):
            result = add_suggestions(rows, "conversation")
        self.assertNotIn("autofill_values", rows[0])
        self.assertEqual(result[0]["autofill_values"], {"body_1": "Amit"})

    def test_patient_chat_introduction_fills_all_three_values(self):
        row = {"body_preview": "Hello {{1}}, This is {{2}} from {{3}} regarding your healthcare request."}
        self.assertEqual(suggest_values(row, {"contact_name": "Amit", "agent_name": "Sam", "account_name": "Clinic"}), {"body_1": "Amit", "body_2": "Sam", "body_3": "Clinic"})

    def test_from_is_not_assumed_to_be_account_without_agent(self):
        self.assertEqual(suggest_values({"body_preview": "Delivery from {{1}}"}, {"account_name": "Clinic"}), {})
