from types import SimpleNamespace
from unittest.mock import patch

import requests
from frappe.tests.utils import FrappeTestCase

from wa_chat_hub.delivery_outcomes import (
    PatientTemplateNotSentError,
    PatientTemplateOutcomeUnknownError,
)
from wa_chat_hub.outbound import send_interakt_message


def account():
    return SimpleNamespace(
        name="Interakt Test",
        interakt_base_url="https://example.invalid/message",
        get_password=lambda fieldname: "secret",
    )


class TestDeliveryOutcomes(FrappeTestCase):
    @patch("wa_chat_hub.outbound.task_log")
    @patch("wa_chat_hub.outbound.requests.post", side_effect=requests.Timeout("timeout"))
    def test_network_timeout_has_unknown_outcome(self, post, task_log):
        with self.assertRaises(PatientTemplateOutcomeUnknownError):
            send_interakt_message(account(), {"payload": {}})

    @patch("wa_chat_hub.outbound.frappe.log_error")
    @patch("wa_chat_hub.outbound.task_log")
    @patch("wa_chat_hub.outbound.requests.post")
    def test_server_rejection_is_retryable_and_definitely_unsent(self, post, task_log, log_error):
        post.return_value = SimpleNamespace(ok=False, status_code=503, text="unavailable")

        with self.assertRaises(PatientTemplateNotSentError) as context:
            send_interakt_message(account(), {"payload": {}})

        self.assertTrue(context.exception.retryable)

    @patch("wa_chat_hub.outbound.frappe.log_error")
    @patch("wa_chat_hub.outbound.task_log")
    @patch("wa_chat_hub.outbound.requests.post")
    def test_bad_request_is_not_automatically_retryable(self, post, task_log, log_error):
        post.return_value = SimpleNamespace(ok=False, status_code=400, text="invalid template")

        with self.assertRaises(PatientTemplateNotSentError) as context:
            send_interakt_message(account(), {"payload": {}})

        self.assertFalse(context.exception.retryable)

