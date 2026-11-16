from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.api.ai_bot import _apply_account_mcp_tool_override, _attempt_explicit_patient_verification
from wa_chat_hub.tests.policy_fixtures import TEST_POLICY


def _route(**overrides):
    values = {
        "party_type": "Patient", "identity_status": "Matched", "patient": "PAT-001",
        "agent_profile": "Patient Care Agent", "requires_patient_data": True,
        "policy_bundle": TEST_POLICY, "allowed_tool_names": {"patient_records"}, "max_tool_calls": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestAutomaticPatientTools(TestCase):
    def test_matched_and_previously_verified_patients_keep_profile_tools(self):
        for status in ("Matched", "Verified"):
            route = _route(identity_status=status)
            _apply_account_mcp_tool_override(route, {"account_tool", "verify_patient_identity"}, 3)
            self.assertEqual(route.allowed_tool_names, {"patient_records", "account_tool"})
            self.assertEqual(route.max_tool_calls, 3)

    def test_ambiguous_and_unverified_patients_cannot_access_protected_tools(self):
        for status in ("Ambiguous", "Unverified"):
            route = _route(identity_status=status, allowed_tool_names={"verify_patient_identity"})
            _apply_account_mcp_tool_override(route, {"account_tool"}, 2)
            self.assertEqual(route.allowed_tool_names, set())
            self.assertEqual(route.max_tool_calls, 0)

    def test_missing_patient_link_does_not_unlock_record_tools(self):
        route = _route(patient=None)
        _apply_account_mcp_tool_override(route, {"account_tool"}, 2)
        self.assertEqual(route.allowed_tool_names, set())

    def test_matched_patient_public_request_keeps_care_tools(self):
        route = _route(requires_patient_data=False)
        _apply_account_mcp_tool_override(route, {"account_tool"}, 3)
        self.assertEqual(route.allowed_tool_names, {"patient_records", "account_tool"})

    def test_lead_preserves_existing_replacement_behavior(self):
        route = _route(party_type="Lead", patient=None, allowed_tool_names={"lead_tool"})
        _apply_account_mcp_tool_override(route, {"account_lead_tool"}, 4)
        self.assertEqual(route.allowed_tool_names, {"account_lead_tool"})
        self.assertEqual(route.max_tool_calls, 4)

    @patch("wa_chat_hub.api.ai_bot.execute_mcp_tool")
    def test_phone_evidence_never_starts_a_verification_tool_call(self, execute):
        for body in ("9000000001", "This is my number", "Show my encounter"):
            result = _attempt_explicit_patient_verification(
                _route(), conversation="CHAT-1", message_id="MSG-1", body_text=body
            )
            self.assertIsNone(result)
        execute.assert_not_called()
