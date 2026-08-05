from datetime import date
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.api.ai_bot import execute_mcp_tool


class TestMCPToolExecution(TestCase):
    @patch("wa_chat_hub.api.ai_bot.log_mcp_event")
    @patch("wa_chat_hub.api.ai_bot.frappe.get_attr")
    @patch("wa_chat_hub.api.ai_bot.fetch_mcp_tools")
    def test_execute_mcp_tool_serializes_dates(
        self, fetch_mcp_tools, get_attr, _log_mcp_event
    ):
        fetch_mcp_tools.return_value = [
            {
                "type": "function",
                "function": {"name": "get_verified_patient_encounters"},
                "_meta": {
                    "url": "wa_chat_hub.mcp.patient_records.get_verified_patient_encounters",
                    "method": "POST",
                    "access_mode": "Read",
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

    @patch("wa_chat_hub.api.ai_bot.log_mcp_event")
    @patch("wa_chat_hub.api.ai_bot.frappe.db.rollback")
    @patch("wa_chat_hub.api.ai_bot.frappe.db.savepoint")
    @patch("wa_chat_hub.api.ai_bot.frappe.get_attr")
    @patch("wa_chat_hub.api.ai_bot.fetch_mcp_tools")
    def test_write_failure_rolls_back_and_hides_internal_error(
        self,
        fetch_mcp_tools,
        get_attr,
        savepoint,
        rollback,
        _log_mcp_event,
    ):
        fetch_mcp_tools.return_value = [
            {
                "type": "function",
                "function": {"name": "create_verified_patient_draft_encounter"},
                "_meta": {
                    "url": "wa_chat_hub.mcp.configured.execute_configured_tool",
                    "method": "POST",
                    "access_mode": "Write",
                },
            }
        ]
        get_attr.side_effect = RuntimeError("private database detail")

        result = execute_mcp_tool(
            "create_verified_patient_draft_encounter",
            {"customer_confirmed": 1},
            allowed_tool_names={"create_verified_patient_draft_encounter"},
            tool_context={"conversation": "CONV-1"},
        )

        savepoint.assert_called_once()
        savepoint_name = savepoint.call_args.args[0]
        self.assertRegex(savepoint_name, r"^wa_mcp_[0-9a-f]{24}$")
        rollback.assert_called_once_with(save_point=savepoint_name)
        self.assertIn("could not be completed safely", result)
        self.assertNotIn("private database detail", result)
