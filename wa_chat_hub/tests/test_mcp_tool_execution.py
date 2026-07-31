from datetime import date
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.api.ai_bot import execute_mcp_tool


class TestMCPToolExecution(TestCase):
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
