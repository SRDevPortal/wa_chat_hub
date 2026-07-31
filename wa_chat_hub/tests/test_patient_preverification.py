from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.api.ai_bot import (
    _apply_account_mcp_tool_override,
    _preverify_matched_patient_route,
)


def _route(**overrides):
    values = {
        "party_type": "Patient",
        "identity_status": "Matched",
        "patient": "PAT-001",
        "agent_profile": "Patient Verification Agent",
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

    def test_non_verified_patient_preserves_existing_replacement_behavior(self):
        route = _route(
            identity_status="Matched",
            allowed_tool_names={"verify_patient_identity"},
            max_tool_calls=1,
        )

        _apply_account_mcp_tool_override(route, {"account_tool"}, 2)

        self.assertEqual(route.allowed_tool_names, {"account_tool"})
        self.assertEqual(route.max_tool_calls, 2)

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

    @patch("wa_chat_hub.api.ai_bot.frappe")
    def test_non_matched_route_is_unchanged(self, frappe):
        route = _route(identity_status="Verified")

        result, can_continue = _preverify_matched_patient_route("CONV-1", route)

        self.assertIs(result, route)
        self.assertTrue(can_continue)
        frappe.db.commit.assert_not_called()

    @patch("wa_chat_hub.api.ai_bot.task_log")
    @patch("wa_chat_hub.api.ai_bot.persist_agent_route")
    @patch("wa_chat_hub.api.ai_bot.resolve_agent_route")
    @patch("wa_chat_hub.identity.verify_patient_identity_by_agent")
    @patch("wa_chat_hub.api.ai_bot.frappe")
    def test_success_re_resolves_to_patient_care(
        self,
        frappe,
        verify,
        resolve,
        persist,
        task_log,
    ):
        matched_route = _route()
        verified_route = _route(
            identity_status="Verified",
            agent_profile="Patient Care Agent",
        )
        verify.return_value = {"verified": True}
        resolve.return_value = verified_route

        result, can_continue = _preverify_matched_patient_route(
            "CONV-1",
            matched_route,
            message_id="MSG-1",
        )

        self.assertIs(result, verified_route)
        self.assertTrue(can_continue)
        verify.assert_called_once_with(patient="PAT-001", conversation="CONV-1")
        persist.assert_called_once_with("CONV-1", verified_route)
        task_log.assert_called_once()
        frappe.db.commit.assert_called_once()

    @patch("wa_chat_hub.api.ai_bot.task_log")
    @patch("wa_chat_hub.api.ai_bot.create_ai_suggestion")
    @patch("wa_chat_hub.identity.verify_patient_identity_by_agent")
    @patch("wa_chat_hub.api.ai_bot.frappe")
    def test_failure_creates_internal_draft_and_stops(
        self,
        frappe,
        verify,
        create_suggestion,
        task_log,
    ):
        route = _route()
        verify.return_value = {
            "verified": False,
            "reason": "patient_phone_mismatch",
        }

        result, can_continue = _preverify_matched_patient_route(
            "CONV-1",
            route,
            message_id="MSG-1",
        )

        self.assertIs(result, route)
        self.assertFalse(can_continue)
        create_suggestion.assert_called_once_with(
            "CONV-1",
            "Support Ticket Draft",
            "Verification failed. Human review required.",
        )
        task_log.assert_called_once()
        frappe.db.commit.assert_called_once()
