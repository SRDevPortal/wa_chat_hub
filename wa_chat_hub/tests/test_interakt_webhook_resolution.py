from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from wa_chat_hub.api import webhook


class TestInteraktWebhookResolution(TestCase):
    def _account(self, name, secret=None, verify_signature=1):
        account = SimpleNamespace(
            name=name,
            interakt_webhook_verify_signature=verify_signature,
        )
        account.get_password = Mock(return_value=secret)
        return account

    def test_missing_secret_does_not_block_another_account_match(self):
        incomplete = self._account("Incomplete Interakt")
        tamil_skin = self._account("Tamil Skin", secret="tamil-secret")

        with (
            patch.object(
                webhook, "_get_interakt_signature_header", return_value="signature"
            ),
            patch.object(
                webhook,
                "safe_ai_get_doc",
                side_effect=[incomplete, tamil_skin],
            ),
            patch.object(
                webhook,
                "_interakt_signature_matches",
                side_effect=lambda secret, _body, _received: secret == "tamil-secret",
            ),
            patch.object(webhook, "task_log"),
        ):
            matches = webhook._match_interakt_channels_by_signature(
                ["Incomplete Interakt", "Tamil Skin"], b"{}"
            )

        self.assertEqual(matches, ["Tamil Skin"])
        incomplete.get_password.assert_called_once_with(
            "interakt_webhook_secret", raise_exception=False
        )

    def test_unreadable_secret_does_not_block_another_account_match(self):
        unreadable = self._account("Unreadable Interakt")
        unreadable.get_password.side_effect = ValueError("cannot decrypt")
        tamil_skin = self._account("Tamil Skin", secret="tamil-secret")

        with (
            patch.object(
                webhook, "_get_interakt_signature_header", return_value="signature"
            ),
            patch.object(
                webhook,
                "safe_ai_get_doc",
                side_effect=[unreadable, tamil_skin],
            ),
            patch.object(
                webhook,
                "_interakt_signature_matches",
                side_effect=lambda secret, _body, _received: secret == "tamil-secret",
            ),
            patch.object(webhook, "task_log"),
        ):
            matches = webhook._match_interakt_channels_by_signature(
                ["Unreadable Interakt", "Tamil Skin"], b"{}"
            )

        self.assertEqual(matches, ["Tamil Skin"])

    def test_signature_disabled_account_does_not_require_secret(self):
        disabled = self._account(
            "Signature Disabled", secret=None, verify_signature=0
        )
        tamil_skin = self._account("Tamil Skin", secret="tamil-secret")

        with (
            patch.object(
                webhook, "_get_interakt_signature_header", return_value="signature"
            ),
            patch.object(
                webhook,
                "safe_ai_get_doc",
                side_effect=[disabled, tamil_skin],
            ),
            patch.object(
                webhook,
                "_interakt_signature_matches",
                side_effect=lambda secret, _body, _received: secret == "tamil-secret",
            ),
        ):
            matches = webhook._match_interakt_channels_by_signature(
                ["Signature Disabled", "Tamil Skin"], b"{}"
            )

        self.assertEqual(matches, ["Tamil Skin"])
        disabled.get_password.assert_not_called()
