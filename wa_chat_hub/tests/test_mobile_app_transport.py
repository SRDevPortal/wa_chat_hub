from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from wa_chat_hub.messaging.windows import evaluate_send_permission
from wa_chat_hub.outbound import send_outbound_message


class TestMobileAppTransport(FrappeTestCase):
    @patch("wa_chat_hub.messaging.windows.safe_ai_get_doc")
    def test_mobile_app_channel_ignores_whatsapp_window(self, get_doc):
        get_doc.side_effect = [
            SimpleNamespace(channel_account="SRIAAS USA Patient App"),
            SimpleNamespace(channel_type="Mobile App"),
        ]

        decision = evaluate_send_permission("CHAT-1", "Text")

        self.assertTrue(decision.allowed)
        self.assertTrue(decision.can_send_free_form)
        self.assertFalse(decision.can_send_template)
        self.assertEqual(decision.reason, "mobile_app_channel")

    @patch("wa_chat_hub.outbound.build_outbound_message_payload")
    @patch("wa_chat_hub.outbound.evaluate_send_permission")
    @patch("wa_chat_hub.outbound.safe_ai_get_doc")
    def test_mobile_app_reply_is_persist_only(
        self, get_doc, evaluate_permission, build_payload
    ):
        get_doc.side_effect = [
            SimpleNamespace(channel_account="SRIAAS USA Patient App"),
            SimpleNamespace(name="SRIAAS USA Patient App", channel_type="Mobile App"),
        ]
        evaluate_permission.return_value = SimpleNamespace(ensure_allowed=lambda _: None)

        result = send_outbound_message("CHAT-1", "Hello", "Text")

        self.assertTrue(result["sent"])
        self.assertEqual(result["transport"], "mobile_app")
        self.assertEqual(result["delivery_status"], "Sent")
        build_payload.assert_not_called()
