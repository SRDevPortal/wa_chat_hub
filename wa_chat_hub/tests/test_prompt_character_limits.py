from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.ai.prompt_limits import (
    fit_messages, get_character_limits, message_characters, shorten_text,
)
from wa_chat_hub.api import ai_bot
from wa_chat_hub.patches.v1_1 import configure_character_limits
from wa_chat_hub.wa_chat_hub.doctype.wa_llm_provider.wa_llm_provider import WALLMProvider


def provider(**overrides):
    values = dict(
        name="Test", provider_type="Custom", model_name="any-model",
        base_url="https://example.invalid/v1", api_key="test-only",
        enable_character_limits=1, max_system_prompt_chars=100,
        max_input_chars=250, max_message_chars=100,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


class TestCharacterLimits(TestCase):
    def test_disabled_preserves_all_text_even_for_previously_restricted_provider(self):
        messages = [{"role": "system", "content": "s" * 10000},
                    {"role": "user", "content": "u" * 2000}]
        result = fit_messages(provider(enable_character_limits=0, base_url="https://vllm.buopso.net"), messages)
        self.assertEqual(result, messages)
        result[-1]["content"] = "changed"
        self.assertEqual(messages[-1]["content"], "u" * 2000)

    def test_enabled_applies_to_any_provider_and_keeps_prompt_ends(self):
        messages = [{"role": "system", "content": "START" + "x" * 1000 + "END"},
                    {"role": "user", "content": "hello"}]
        result = fit_messages(provider(model_name="gpt-4o-mini"), messages)
        self.assertEqual(len(result[0]["content"]), 100)
        self.assertTrue(result[0]["content"].startswith("START"))
        self.assertTrue(result[0]["content"].endswith("END"))
        self.assertIn("shortened", result[0]["content"])

    def test_unicode_count_and_notice_stay_within_limit(self):
        for limit in (1, 10, 50, 100):
            text = shorten_text("नमस्ते🙂" * 100, limit)
            self.assertEqual(len(text), limit)

    def test_latest_turn_wins_and_source_is_unchanged(self):
        messages = [{"role": "system", "content": "s" * 100},
                    {"role": "user", "content": "old" * 30},
                    {"role": "assistant", "content": "answer" * 15},
                    {"role": "user", "content": "latest" * 20}]
        original = deepcopy(messages)
        result = fit_messages(provider(), messages)
        self.assertEqual(len(result), 2)
        self.assertTrue(result[-1]["content"].startswith("latest"))
        self.assertLessEqual(sum(map(message_characters, result)), 250)
        self.assertEqual(messages, original)

    def test_increasing_provider_limit_retains_long_history(self):
        history = [frappe._dict(direction="Inbound", body="h" * 2500, content_type="Text")]
        selected = provider(max_system_prompt_chars=1000, max_input_chars=8000, max_message_chars=3000)
        with patch.object(ai_bot, "provider_endpoint", return_value="https://example.invalid/v1"), \
             patch.object(ai_bot, "provider_timeout", return_value=20), \
             patch.object(ai_bot, "call_openai_format") as send:
            ai_bot.call_provider(selected, "system", history, latest_user_text="latest")
        result = fit_messages(selected, send.call_args.args[1])
        self.assertEqual(result[1]["content"], "h" * 2500)

    def test_disabled_history_is_not_shortened_before_sending(self):
        history = [frappe._dict(direction="Inbound", body="h" * 2500, content_type="Text")]
        with patch.object(ai_bot, "provider_endpoint", return_value="https://example.invalid/v1"), \
             patch.object(ai_bot, "provider_timeout", return_value=20), \
             patch.object(ai_bot, "call_openai_format") as send:
            ai_bot.call_provider(provider(enable_character_limits=0), "system", history)
        self.assertEqual(send.call_args.args[1][1]["content"], "h" * 2500)

    def test_images_survive_text_trimming(self):
        content = [{"type": "text", "text": "x" * 200},
                   {"type": "image_url", "image_url": {"url": "https://example.invalid/image"}}]
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": content}]
        result = fit_messages(provider(), messages)
        self.assertEqual(result[-1]["content"][1], content[1])
        self.assertEqual(len(result[-1]["content"][0]["text"]), 100)

    def test_tool_followup_keeps_pairs_and_fits_total(self):
        call = {"id": "call1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        messages = [{"role": "system", "content": "s" * 100},
                    {"role": "user", "content": "question" * 30},
                    {"role": "assistant", "content": None, "tool_calls": [call]},
                    {"role": "tool", "tool_call_id": "call1", "content": "result" * 100}]
        result = fit_messages(provider(max_input_chars=300), messages)
        self.assertEqual([m["role"] for m in result], ["system", "user", "assistant", "tool"])
        self.assertEqual(result[2]["tool_calls"], [call])
        self.assertEqual(result[3]["tool_call_id"], "call1")
        self.assertLessEqual(sum(map(message_characters, result)), 300)
        self.assertTrue(result[1]["content"])
        self.assertTrue(result[3]["content"])

    def test_oversized_tool_arguments_fail_instead_of_corrupting_call(self):
        messages = [{"role": "system", "content": "s"},
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "tool_calls": [{"function": {"arguments": "x" * 1000}}]},
                    {"role": "tool", "content": "result"}]
        with self.assertRaisesRegex(ValueError, "too small"):
            fit_messages(provider(), messages)

    def test_saved_values_are_validated(self):
        for value in (0, -1, None, "", "2.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                get_character_limits(provider(max_system_prompt_chars=value))
        with self.assertRaisesRegex(ValueError, "must exceed"):
            get_character_limits(provider(max_input_chars=100))
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            get_character_limits(provider(max_message_chars=251))
        self.assertIsNone(get_character_limits(provider(enable_character_limits=0, max_input_chars=0)))

    def test_controller_rejects_invalid_settings(self):
        with patch("wa_chat_hub.wa_chat_hub.doctype.wa_llm_provider.wa_llm_provider.frappe.throw", side_effect=ValueError) as error:
            with self.assertRaises(ValueError):
                WALLMProvider.validate(provider(max_input_chars=100))
        error.assert_called_once()

    def test_fallback_providers_fit_independently(self):
        messages = [{"role": "system", "content": "s" * 1000}, {"role": "user", "content": "u" * 500}]
        fit_messages(provider(), messages)
        self.assertEqual(fit_messages(provider(enable_character_limits=0), messages), messages)

    def test_reloading_provider_reads_saved_controls(self):
        row = frappe._dict(name="Test", provider_type="Custom", model_name="model", base_url="https://example.invalid")
        doc = frappe._dict(vars(provider()))
        doc.get_password = lambda *a, **k: "test-only"
        with patch.object(ai_bot, "get_active_llm_provider_rows", return_value=[row]), \
             patch.object(ai_bot, "safe_ai_get_doc", return_value=doc):
            first = ai_bot._load_providers()[0]
            doc.max_system_prompt_chars = 200
            second = ai_bot._load_providers()[0]
        self.assertEqual(first.max_system_prompt_chars, 100)
        self.assertEqual(second.max_system_prompt_chars, 200)

    def test_migration_seeds_once_and_preserves_explicit_choices(self):
        rows = [frappe._dict(name="Gemma", base_url="https://vllm.buopso.net/v1", model_name="gemma", character_limits_version=0),
                frappe._dict(name="OpenAI", base_url="https://api.openai.com/v1", model_name="gpt", character_limits_version=0),
                frappe._dict(name="Edited", base_url="https://vllm.buopso.net", model_name="glm", character_limits_version=1)]
        with patch.object(configure_character_limits, "frappe") as api:
            api.get_all.return_value = rows
            api.get_meta.return_value.get_field.return_value.default = "100"
            configure_character_limits.execute()
        calls = api.db.set_value.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].args[2]["enable_character_limits"], 1)
        self.assertEqual(calls[1].args[2]["enable_character_limits"], 0)
        self.assertEqual(calls[0].args[2]["max_input_chars"], 100)

    def test_followup_http_request_is_also_fitted(self):
        call = {"id": "call1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        responses = [SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"choices": [{"message": {
            "role": "assistant", "content": None, "tool_calls": [call]}}]}),
            SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: {"choices": [{"message": {"content": "done"}}]})]
        payloads = []
        def post(*args, **kwargs):
            payloads.append(deepcopy(kwargs["json"]))
            return responses.pop(0)
        with patch.object(ai_bot, "provider_endpoint", return_value="https://example.invalid/v1"), \
             patch.object(ai_bot, "fetch_mcp_tools", return_value=[]), \
             patch.object(ai_bot, "execute_mcp_tool", return_value="result" * 200), \
             patch.object(ai_bot, "_log_ai_timing"), \
             patch.object(ai_bot.requests, "post", side_effect=post):
            ai_bot.call_openai_format(provider(max_input_chars=300),
                [{"role": "system", "content": "s" * 100}, {"role": "user", "content": "question"}],
                max_tool_calls=1)
        self.assertEqual(len(payloads), 2)
        for payload in payloads:
            self.assertLessEqual(sum(map(message_characters, payload["messages"])), 300)
        self.assertEqual(payloads[1]["messages"][-1]["tool_call_id"], "call1")
