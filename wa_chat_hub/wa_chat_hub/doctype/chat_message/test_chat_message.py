# Copyright (c) 2026, SAI and Contributors
# See license.txt

from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from wa_chat_hub.wa_chat_hub.doctype.chat_message.chat_message import ChatMessage


class TestChatMessage(FrappeTestCase):
	@patch(
		"wa_chat_hub.wa_chat_hub.doctype.chat_message.chat_message.build_message_dedupe_key",
		return_value="dedupe-key",
	)
	@patch(
		"wa_chat_hub.wa_chat_hub.doctype.chat_message.chat_message.webhook_idempotency_enabled",
		return_value=True,
	)
	@patch(
		"wa_chat_hub.wa_chat_hub.doctype.chat_message.chat_message.now_datetime",
		return_value="2026-07-14 12:00:00",
	)
	def test_before_validate_sets_received_time_and_dedupe_key(self, _now, _enabled, build_key):
		doc = SimpleNamespace(
			received_at=None,
			dedupe_key=None,
			conversation="CONV-1",
			provider_name="Meta",
			provider_message_id="wamid.1",
			channel_message_id=None,
			provider_event_id="event-1",
		)

		ChatMessage.before_validate(doc)

		self.assertEqual(doc.received_at, "2026-07-14 12:00:00")
		self.assertEqual(doc.dedupe_key, "dedupe-key")
		build_key.assert_called_once_with(
			conversation="CONV-1",
			provider_name="Meta",
			provider_message_id="wamid.1",
			channel_message_id=None,
			provider_event_id="event-1",
		)

	@patch(
		"wa_chat_hub.wa_chat_hub.doctype.chat_message.chat_message.webhook_idempotency_enabled",
		return_value=True,
	)
	def test_before_validate_preserves_existing_values(self, _enabled):
		doc = SimpleNamespace(received_at="existing-time", dedupe_key="existing-key")

		ChatMessage.before_validate(doc)

		self.assertEqual(doc.received_at, "existing-time")
		self.assertEqual(doc.dedupe_key, "existing-key")
