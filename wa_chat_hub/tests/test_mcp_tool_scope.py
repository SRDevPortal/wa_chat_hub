from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from frappe import _dict

from wa_chat_hub.api import ai_bot


class TestMCPToolScope(TestCase):
    def _tool(self, name, company):
        return _dict(
            tool_name=name,
            description=name,
            parameters_schema='{"type":"object","properties":{}}',
            endpoint_url="/api/method/frappe.client.get_list",
            http_method="POST",
            server=f"{company or 'shared'} MCP",
            company=company,
        )

    def test_fetch_tools_requires_matching_company_and_prompt_name(self):
        rows = [
            self._tool("sriaas_allowed_tool", "sriaas"),
            self._tool("sriaas_unmentioned_tool", "sriaas"),
            self._tool("globifit_write_tool", "globifit"),
            self._tool("shared_unmentioned_tool", ""),
        ]
        with (
            patch.object(ai_bot, "assert_ai_doctype_permission"),
            patch.object(
                ai_bot.frappe,
                "get_single",
                return_value=SimpleNamespace(allow_mcp_access=1),
            ),
            patch.object(ai_bot, "safe_ai_exists", return_value=True),
            patch.object(ai_bot, "safe_ai_get_all", return_value=rows),
        ):
            tools = ai_bot.fetch_mcp_tools(
                company="sriaas",
                system_prompt="Use sriaas_allowed_tool for this account.",
            )

        self.assertEqual(
            [tool["function"]["name"] for tool in tools],
            ["sriaas_allowed_tool"],
        )

    def test_execute_rejects_tool_outside_scoped_set(self):
        with (
            patch.object(ai_bot, "fetch_mcp_tools", return_value=[]),
            patch.object(ai_bot, "log_agent_event") as log_event,
        ):
            result = ai_bot.execute_mcp_tool(
                "globifit_write_tool",
                {"doc": {"doctype": "Patient"}},
                company="sriaas",
                system_prompt="Use only sriaas_allowed_tool.",
            )

        self.assertIn("not found", result)
        log_event.assert_called_once()

