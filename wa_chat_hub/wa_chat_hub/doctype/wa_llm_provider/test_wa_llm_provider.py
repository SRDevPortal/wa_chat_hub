# Copyright (c) 2026, SAI and Contributors
# See license.txt

from frappe import _dict
from frappe.tests.utils import FrappeTestCase

from wa_chat_hub.ai.lead_scoring import _is_chat_reply_only_provider
from wa_chat_hub.setup_autopilot import (
	BUOPSO_VLLM_CHAT_COMPLETIONS_URL,
	BUOPSO_VLLM_MODEL,
	BUOPSO_VLLM_PROVIDER_TITLE,
)


class TestWALLMProvider(FrappeTestCase):
	def test_buopso_vllm_provider_defaults_are_declared(self):
		self.assertEqual(BUOPSO_VLLM_PROVIDER_TITLE, "Buopso vLLM Qwen")
		self.assertEqual(BUOPSO_VLLM_CHAT_COMPLETIONS_URL, "https://vllm.buopso.net/v1/chat/completions")
		self.assertEqual(BUOPSO_VLLM_MODEL, "qwen3:4b")

	def test_buopso_vllm_is_chat_reply_only_for_scoring(self):
		row = _dict({
			"model_name": BUOPSO_VLLM_MODEL,
			"base_url": BUOPSO_VLLM_CHAT_COMPLETIONS_URL,
		})

		self.assertTrue(_is_chat_reply_only_provider(row))
