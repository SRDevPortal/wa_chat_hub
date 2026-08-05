from __future__ import annotations

import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.ai.workflow_engine import (
    _next_missing_collect_index,
    _prefill_candidates,
    _prefill_value,
    _valid_collected_value,
)
from wa_chat_hub.mcp.configured import _resolve_value


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _record(filename: str, doctype: str, name_field: str, name: str) -> dict:
    payload = json.loads((CONFIG_DIR / filename).read_text(encoding="utf-8"))
    return next(row for row in payload[doctype] if row[name_field] == name)


class TestConfigurablePatientCreationFlow(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = _record(
            "default_ai_routing.json",
            "WA AI Workflow",
            "workflow_name",
            "Treatment Case Creation",
        )
        cls.endpoint = _record(
            "default_mcp_endpoints.json",
            "WA MCP Tool Endpoint",
            "tool_name",
            "create_verified_patient_draft_encounter",
        )
        cls.policy = _record(
            "default_policy_bundle.json",
            "WA AI Policy Bundle",
            "policy_name",
            "Default Configurable WhatsApp Policy",
        )
        cls.verification_route = _record(
            "default_ai_routing.json",
            "WA AI Intent Route",
            "route_name",
            "Protected Matched Patient",
        )

    def test_explicit_name_in_recent_chat_prefills_configured_step(self):
        name_step = self.workflow["definition"]["collect"][0]

        value = _prefill_value(
            name_step["prefill"],
            ["I need treatment", "My name is Amit Sharma."],
        )

        self.assertEqual(value, "Amit Sharma")
        self.assertTrue(_valid_collected_value(value, name_step["validation"]))

    def test_missing_name_keeps_name_as_first_collection_step(self):
        collect = self.workflow["definition"]["collect"]

        self.assertEqual(_prefill_value(collect[0]["prefill"], ["I need treatment"]), "")
        self.assertEqual(_next_missing_collect_index(collect, {}, 0), 0)
        self.assertEqual(
            _next_missing_collect_index(collect, {"customer_name": "Amit Sharma"}, 0),
            1,
        )

    @patch("wa_chat_hub.ai.workflow_engine.safe_ai_get_all")
    @patch("wa_chat_hub.ai.workflow_engine.safe_ai_get_value")
    def test_valid_phone_contact_name_has_priority_over_chat_name(
        self, get_value, get_all
    ):
        name_step = self.workflow["definition"]["collect"][0]
        get_value.side_effect = [
            frappe._dict(first_name="👉️", last_name=None),
            frappe._dict(lead_name="👉️"),
        ]
        get_all.return_value = [
            frappe._dict(name="CONTACT-PLACEHOLDER", first_name="👉️", last_name=None),
            frappe._dict(name="CONTACT-AMIT", first_name="Amit", last_name="Yadav"),
        ]

        candidates = _prefill_candidates(
            name_step["prefill"],
            ["My name is Chat Name"],
            {"linked_crm_lead": "CRM-LEAD-1", "phone_number": "919466073244"},
        )
        valid = [
            value
            for value in candidates
            if _valid_collected_value(value, name_step["validation"])
        ]

        self.assertEqual(valid[0], "Amit Yadav")

    def test_placeholder_without_letters_is_not_a_valid_name(self):
        validation = self.workflow["definition"]["collect"][0]["validation"]

        self.assertFalse(_valid_collected_value("👉️", validation))

    def test_gender_default_is_policy_owned(self):
        creation_policy = self.policy["patient_creation_policy"]

        self.assertEqual(
            creation_policy["demographic_defaults"]["sex"],
            "Prefer not to say",
        )

    def test_patient_name_and_department_are_resolved_from_config(self):
        patient_values = self.endpoint["execution_config"]["resolve_patient"]["patient_field_values"]
        context = {
            "channel_account_doc": frappe._dict(
                default_medical_department="Configured Medical Department"
            )
        }
        args = {"customer_name": "Amit Sharma"}
        department_spec = dict(patient_values["sr_medical_department"])
        department_spec.pop("doctype", None)

        self.assertEqual(_resolve_value(patient_values["first_name"], args, context), "Amit Sharma")
        self.assertEqual(
            _resolve_value(department_spec, args, context),
            "Configured Medical Department",
        )

    def test_workflow_authorizes_customer_name_argument(self):
        definition = self.workflow["definition"]
        schema = self.endpoint["parameters_schema"]

        self.assertEqual(definition["arguments"]["customer_name"]["field"], "customer_name")
        self.assertIn("customer_name", schema["required"])
        self.assertIn("customer_name", self.endpoint["execution_config"]["required_arguments"])

    def test_verification_prompt_requests_only_registered_phone(self):
        expected = (
            "Verification is required before I can access your personal information. "
            "Please share your registered phone number."
        )

        self.assertEqual(self.verification_route["blocked_reply"], expected)
        self.assertEqual(self.verification_route["blocked_replies"]["en"], expected)
        self.assertNotIn("this is my number", expected.lower())

    def test_current_number_claim_remains_an_optional_policy_input(self):
        phrases = self.policy["identity_policy"]["current_number_claim_phrases"]

        self.assertIn("this is my number", phrases["en"])
        self.assertIn("ye mera no h", phrases["hi-latn"])
