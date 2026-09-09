from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.api.ai_bot import (
    _apply_account_mcp_tool_override,
    _attempt_explicit_patient_verification,
)
from wa_chat_hub.tests.policy_fixtures import TEST_POLICY


def _route(**overrides):
    values = {
        "party_type": "Patient",
        "identity_status": "Matched",
        "patient": "PAT-001",
        "agent_profile": "Patient Verification Agent",
        "requires_patient_data": True,
        "policy_bundle": TEST_POLICY,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestPatientPreverification(TestCase):
    def test_verified_patient_keeps_profile_tools_and_adds_account_tools(self):
        route = _route(
            identity_status="Verified",
            agent_profile="Patient Care Agent",
            allowed_tool_names={"get_verified_patient_sales_invoices"},
            max_tool_calls=2,
        )

        _apply_account_mcp_tool_override(
            route,
            {"create_verified_patient_draft_encounter"},
            3,
        )

        self.assertEqual(
            route.allowed_tool_names,
            {
                "get_verified_patient_sales_invoices",
                "create_verified_patient_draft_encounter",
            },
        )
        self.assertEqual(route.max_tool_calls, 3)

    def test_non_verified_patient_rejects_account_tools(self):
        route = _route(
            identity_status="Matched",
            allowed_tool_names={"verify_patient_identity"},
            max_tool_calls=1,
        )

        _apply_account_mcp_tool_override(route, {"account_tool"}, 2)

        self.assertEqual(route.allowed_tool_names, {"verify_patient_identity"})
        self.assertEqual(route.max_tool_calls, 1)


    def test_matched_patient_public_request_uses_account_tools(self):
        route = _route(
            requires_patient_data=False,
            allowed_tool_names=set(),
            max_tool_calls=0,
        )

        _apply_account_mcp_tool_override(
            route,
            {"create_verified_patient_draft_encounter", "get_linked_lead_profile"},
            3,
        )

        self.assertEqual(
            route.allowed_tool_names,
            {"create_verified_patient_draft_encounter", "get_linked_lead_profile"},
        )
        self.assertEqual(route.max_tool_calls, 3)

    def test_lead_preserves_existing_replacement_behavior(self):
        route = _route(
            party_type="Lead",
            identity_status="Matched",
            patient=None,
            allowed_tool_names={"lead_profile_tool"},
            max_tool_calls=1,
        )

        _apply_account_mcp_tool_override(route, {"account_lead_tool"}, 4)

        self.assertEqual(route.allowed_tool_names, {"account_lead_tool"})
        self.assertEqual(route.max_tool_calls, 4)

    def test_matched_patient_keeps_only_verification_tool(self):
        route = _route(
            allowed_tool_names={"verify_patient_identity"},
            max_tool_calls=1,
        )

        _apply_account_mcp_tool_override(
            route,
            {"create_verified_patient_draft_encounter", "get_linked_lead_profile"},
            3,
        )

        self.assertEqual(route.allowed_tool_names, {"verify_patient_identity"})
        self.assertEqual(route.max_tool_calls, 1)
    def test_each_verification_option_independently_executes_the_tool(self):
        route = _route(
            department=None,
            allowed_tool_names={"verify_patient_identity"},
            max_tool_calls=1,
        )
        for body_text in ("9000000001", "This is my number"):
            with self.subTest(body_text=body_text), patch(
                "wa_chat_hub.api.ai_bot.execute_mcp_tool",
                return_value=(
                    '{"verified": true, "reason": "matched", "patient": "PAT-001"}'
                ),
            ) as execute:
                result = _attempt_explicit_patient_verification(
                    route,
                    conversation="CONV-1",
                    message_id="MSG-1",
                    body_text=body_text,
                )

            self.assertTrue(result["verified"])
            execute.assert_called_once_with(
                "verify_patient_identity",
                {},
                allowed_tool_names={"verify_patient_identity"},
                tool_context={
                    "conversation": "CONV-1",
                    "patient": "PAT-001",
                    "agent_profile": "Patient Verification Agent",
                    "department": None,
                    "identity_status": "Matched",
                    "message": "MSG-1",
                },
            )

    @patch("wa_chat_hub.api.ai_bot.execute_mcp_tool")
    def test_no_verification_evidence_does_not_execute_the_tool(self, execute):
        route = _route(
            department=None,
            allowed_tool_names={"verify_patient_identity"},
            max_tool_calls=1,
        )

        result = _attempt_explicit_patient_verification(
            route,
            conversation="CONV-1",
            message_id="MSG-1",
            body_text="please show my encounter",
        )

        self.assertIsNone(result)
        execute.assert_not_called()

    @patch("wa_chat_hub.api.ai_bot.execute_mcp_tool")
    def test_evidence_cannot_bypass_a_missing_tool_allowlist(self, execute):
        route = _route(
            department=None,
            allowed_tool_names=set(),
            max_tool_calls=0,
        )

        result = _attempt_explicit_patient_verification(
            route,
            conversation="CONV-1",
            message_id="MSG-1",
            body_text="9000000001",
        )

        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "verification_tool_unavailable")
        execute.assert_not_called()
