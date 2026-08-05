from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.ai.workflow_engine import _challenge_digest, _confirmation_candidates
from wa_chat_hub.api.webhook import _process_interakt_quick_reply


class TestInteraktWorkflowConfirmation(TestCase):
    def test_cta_click_never_becomes_an_authorizing_message(self):
        payload = {
            "channel_account": "ACCOUNT-1",
            "data": {
                "customer": {"channel_phone_number": "+919000000002"},
                "message": {"click_type": "CTA", "button_text": "Open site"},
            },
        }
        with patch("wa_chat_hub.api.webhook.append_message") as append:
            result = _process_interakt_quick_reply(payload, {})
        self.assertTrue(result["ignored"])
        append.assert_not_called()

    @patch("wa_chat_hub.api.webhook._assert_webhook_message_write_permissions")
    @patch("wa_chat_hub.api.webhook.set_ai_security_context")
    @patch("wa_chat_hub.api.webhook.append_message")
    def test_quick_reply_is_persisted_with_structured_transport_payload(
        self, append, _security_context, _permissions
    ):
        append.return_value = {"conversation": "CONV-1", "message": "MSG-CLICK"}
        payload = {
            "channel_account": "ACCOUNT-1",
            "data": {
                "customer": {
                    "channel_phone_number": "+919000000002",
                    "traits": {"name": "Example Patient"},
                },
                "message": {
                    "click_type": "QR",
                    "button_text": "Confirm",
                    "meta_data": {
                        "source_data": {"callback_data": "opaque-one-time-token"}
                    },
                },
            },
        }
        result = _process_interakt_quick_reply(payload, {"channel_department": "Support"})
        self.assertTrue(result["quick_reply"])
        values = append.call_args.args[0]
        self.assertEqual(values["body"], "Confirm")
        self.assertEqual(values["raw_transport_payload"], payload)
        self.assertTrue(values["channel_message_id"].startswith("interakt-click-"))

    def test_nested_callback_token_can_satisfy_only_its_workflow_instance(self):
        state = {"instance_id": "workflow-instance"}
        token = "opaque-one-time-token"
        state["interaction_digest"] = _challenge_digest(state, token)
        candidates = _confirmation_candidates(
            "Confirm",
            {
                "data": {
                    "message": {
                        "meta_data": {
                            "source_data": {
                                "callback_data": token,
                                "buttonPayload": {"payload": {"type": "text", "text": token}},
                            }
                        }
                    }
                }
            },
        )
        self.assertIn(token, candidates)
        self.assertNotEqual(
            _challenge_digest({"instance_id": "another-workflow"}, token),
            state["interaction_digest"],
        )
