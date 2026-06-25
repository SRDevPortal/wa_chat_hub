# Copyright (c) 2026, SAI and Contributors
# See license.txt

# import frappe
from frappe.tests.utils import FrappeTestCase
from frappe._dict import _dict

from wa_chat_hub.ai.lead_scoring import _is_chat_reply_only_provider


class TestWALLMProvider(FrappeTestCase):
	def test_buopso_vllm_is_chat_reply_only_for_scoring(self):
		row = _dict({
			"model_name": "qwen3:4b",
			"base_url": "https://vllm.buopso.net/v1/chat/completions",
		})

		self.assertTrue(_is_chat_reply_only_provider(row))
