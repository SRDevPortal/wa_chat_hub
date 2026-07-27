import copy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.api import ai_bot
from wa_chat_hub.api.ai_bot import (
    _is_read_only_mcp_tool,
    _require_followup_mcp_tool,
    _required_mcp_tool_choice,
    _requires_encounter_detail,
    _requires_mcp_lookup,
)


class TestMCPLookupEnforcement(TestCase):
    def _messages(self, text):
        return [
            {"role": "system", "content": "Use account tools."},
            {"role": "user", "content": text},
        ]

    def test_last_order_requires_mcp_and_encounter_detail(self):
        messages = self._messages("What is my last order?")

        self.assertTrue(_requires_mcp_lookup(messages))
        self.assertTrue(_requires_encounter_detail(messages))

    def test_returning_customer_question_requires_mcp(self):
        messages = self._messages("Do you know me?")

        self.assertTrue(_requires_mcp_lookup(messages))
        self.assertFalse(_requires_encounter_detail(messages))

    def test_general_skin_question_does_not_force_mcp(self):
        messages = self._messages("What can cause dry skin?")

        self.assertFalse(_requires_mcp_lookup(messages))
        self.assertFalse(_requires_encounter_detail(messages))

    def test_order_lookup_requires_tools_until_encounter_detail(self):
        self.assertTrue(
            _require_followup_mcp_tool(
                True,
                ["sriaas_find_tamil_skin_patient"],
                0,
            )
        )
        self.assertTrue(
            _require_followup_mcp_tool(
                True,
                [
                    "sriaas_find_tamil_skin_patient",
                    "sriaas_find_tamil_skin_encounters",
                ],
                1,
            )
        )
        self.assertFalse(
            _require_followup_mcp_tool(
                True,
                [
                    "sriaas_find_tamil_skin_patient",
                    "sriaas_find_tamil_skin_encounters",
                    "sriaas_get_tamil_skin_encounter",
                ],
                2,
            )
        )

    def test_customer_lookup_excludes_write_tools(self):
        self.assertTrue(_is_read_only_mcp_tool("sriaas_find_tamil_skin_patient"))
        self.assertTrue(_is_read_only_mcp_tool("sriaas_get_tamil_skin_encounter"))
        self.assertFalse(_is_read_only_mcp_tool("sriaas_create_tamil_skin_patient"))
        self.assertFalse(
            _is_read_only_mcp_tool("sriaas_create_tamil_skin_draft_encounter")
        )

    def test_order_lookup_selects_patient_then_encounter_tools(self):
        tools = [
            {
                "type": "function",
                "function": {"name": "sriaas_find_tamil_skin_patient"},
            },
            {
                "type": "function",
                "function": {"name": "sriaas_find_tamil_skin_encounters"},
            },
            {
                "type": "function",
                "function": {"name": "sriaas_get_tamil_skin_encounter"},
            },
        ]

        first = _required_mcp_tool_choice(tools, [], True)
        second = _required_mcp_tool_choice(
            tools,
            ["sriaas_find_tamil_skin_patient"],
            True,
        )
        third = _required_mcp_tool_choice(
            tools,
            [
                "sriaas_find_tamil_skin_patient",
                "sriaas_find_tamil_skin_encounters",
            ],
            True,
        )

        self.assertEqual(
            first["function"]["name"],
            "sriaas_find_tamil_skin_patient",
        )
        self.assertEqual(
            second["function"]["name"],
            "sriaas_find_tamil_skin_encounters",
        )
        self.assertEqual(
            third["function"]["name"],
            "sriaas_get_tamil_skin_encounter",
        )

    def test_provider_forces_read_only_order_lookup_sequence(self):
        tool_names = [
            "sriaas_find_tamil_skin_patient",
            "sriaas_find_tamil_skin_encounters",
            "sriaas_get_tamil_skin_encounter",
        ]
        tools = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
                "_meta": {},
            }
            for name in tool_names
        ]
        response_messages = [
            self._tool_call_message("call-1", tool_names[0]),
            self._tool_call_message("call-2", tool_names[1]),
            self._tool_call_message("call-3", tool_names[2]),
            {"role": "assistant", "content": "Your latest order details."},
        ]
        payloads = []

        def post(_url, headers=None, json=None, timeout=None):
            payloads.append(copy.deepcopy(json))
            return self._response(response_messages.pop(0))

        provider = SimpleNamespace(
            name="OpenAI Default",
            provider_type="OpenAI",
            model_name="gpt-4o-mini",
            base_url="https://api.openai.com/v1/chat/completions",
            api_key="test",
        )
        with (
            patch.object(ai_bot, "fetch_mcp_tools", return_value=tools),
            patch.object(
                ai_bot,
                "execute_mcp_tool",
                return_value='{"message": []}',
            ) as execute,
            patch.object(ai_bot.requests, "post", side_effect=post),
            patch.object(ai_bot, "_log_ai_timing"),
        ):
            result = ai_bot.call_openai_format(
                provider,
                self._messages("What is my last order?"),
                company="sriaas",
            )

        self.assertEqual(result, "Your latest order details.")
        self.assertEqual(
            [
                payloads[index]["tool_choice"]["function"]["name"]
                for index in range(3)
            ],
            tool_names,
        )
        self.assertNotIn("tool_choice", payloads[3])
        self.assertEqual(
            [call.args[0] for call in execute.call_args_list],
            tool_names,
        )

    def _tool_call_message(self, call_id, tool_name):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": "{}"},
                }
            ],
        }

    def _response(self, message):
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {"choices": [{"message": message}]},
            raise_for_status=lambda: None,
        )
