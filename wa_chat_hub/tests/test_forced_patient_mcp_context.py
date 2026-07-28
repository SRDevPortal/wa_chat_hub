from types import SimpleNamespace
from datetime import date
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.api.ai_bot import (
    build_forced_patient_mcp_context,
    execute_mcp_tool,
    _forced_patient_mcp_tools,
)


class TestForcedPatientMCPContext(TestCase):
    def test_medical_history_selects_patient_record_tools(self):
        tools = _forced_patient_mcp_tools(
            "Can I get my medical history?",
            {
                "get_verified_patient_profile",
                "get_verified_patient_encounters",
                "get_verified_patient_diet_charts",
            },
        )

        self.assertEqual(
            tools,
            [
                "get_verified_patient_profile",
                "get_verified_patient_encounters",
                "get_verified_patient_diet_charts",
            ],
        )

    def test_shipping_status_selects_shipping_history_tool(self):
        tools = _forced_patient_mcp_tools(
            "Can you tell me my delivery status and AWB tracking?",
            {
                "get_verified_patient_sales_invoices",
                "get_verified_patient_shipping_history",
            },
        )

        self.assertEqual(tools, ["get_verified_patient_shipping_history"])

    @patch("wa_chat_hub.api.ai_bot.execute_mcp_tool")
    def test_verified_patient_record_request_fetches_mcp_context(self, execute_mcp_tool):
        execute_mcp_tool.return_value = '{"patient":"PAT-001","encounters":[{"name":"ENC-1"}]}'
        route = SimpleNamespace(
            party_type="Patient",
            identity_status="Verified",
            patient="PAT-001",
            agent_profile="Patient Care Agent",
            department="Nephrology",
        )

        context, tools = build_forced_patient_mcp_context(
            route,
            "CONV-1",
            "Mujhe meri clinical history btao",
            allowed_tool_names={"get_verified_patient_encounters"},
        )

        self.assertEqual(tools, ["get_verified_patient_encounters"])
        self.assertIn("MANDATORY VERIFIED PATIENT RECORD CONTEXT", context)
        self.assertIn("ENC-1", context)
        execute_mcp_tool.assert_called_once()

    @patch("wa_chat_hub.api.ai_bot.execute_mcp_tool")
    def test_unverified_patient_does_not_fetch_mcp_context(self, execute_mcp_tool):
        route = SimpleNamespace(
            party_type="Patient",
            identity_status="Matched",
            patient="PAT-001",
            agent_profile="Patient Verification Agent",
            department="Nephrology",
        )

        context, tools = build_forced_patient_mcp_context(
            route,
            "CONV-1",
            "Can I get my medical history?",
            allowed_tool_names={"get_verified_patient_encounters"},
        )

        self.assertEqual(context, "")
        self.assertEqual(tools, [])
        execute_mcp_tool.assert_not_called()

    @patch("wa_chat_hub.api.ai_bot.frappe.get_attr")
    @patch("wa_chat_hub.api.ai_bot.fetch_mcp_tools")
    def test_execute_mcp_tool_serializes_dates(self, fetch_mcp_tools, get_attr):
        fetch_mcp_tools.return_value = [
            {
                "type": "function",
                "function": {"name": "get_verified_patient_encounters"},
                "_meta": {
                    "url": "wa_chat_hub.mcp.patient_records.get_verified_patient_encounters",
                    "method": "POST",
                },
            }
        ]
        get_attr.return_value = lambda **_kwargs: {
            "encounters": [{"encounter_date": date(2026, 7, 28)}]
        }

        result = execute_mcp_tool(
            "get_verified_patient_encounters",
            {},
            allowed_tool_names={"get_verified_patient_encounters"},
            tool_context={"patient": "PAT-1", "conversation": "CONV-1"},
        )

        self.assertIn("2026-07-28", result)
        self.assertNotIn("not JSON serializable", result)
