from __future__ import annotations

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from wa_chat_hub.patches.v1_0 import (
    activate_conversation_job_deduplication_setting,
)
from wa_chat_hub.performance_flags import (
    conversation_job_deduplication_enabled,
    conversation_job_enqueue_options,
)


class _Meta:
    def __init__(self, fields):
        self.fields = set(fields)

    def has_field(self, fieldname):
        return fieldname in self.fields


def _settings(*, enabled=0, active=0, include_active_field=True):
    fields = {"enable_conversation_job_deduplication"}
    if include_active_field:
        fields.add("conversation_job_deduplication_setting_active")
    return SimpleNamespace(
        meta=_Meta(fields),
        enable_conversation_job_deduplication=enabled,
        conversation_job_deduplication_setting_active=active,
    )


class TestConversationJobDeduplicationPolicy(TestCase):
    @patch("wa_chat_hub.performance_flags.frappe.get_cached_doc")
    def test_pre_migration_site_preserves_existing_enabled_behavior(self, get_settings):
        get_settings.return_value = _settings(
            enabled=0,
            active=0,
            include_active_field=False,
        )

        self.assertTrue(conversation_job_deduplication_enabled())

    @patch("wa_chat_hub.performance_flags.frappe.get_cached_doc")
    def test_inactive_marker_preserves_existing_enabled_behavior(self, get_settings):
        get_settings.return_value = _settings(enabled=0, active=0)

        self.assertTrue(conversation_job_deduplication_enabled())

    @patch("wa_chat_hub.performance_flags.frappe.get_cached_doc")
    def test_activated_setting_can_disable_deduplication(self, get_settings):
        get_settings.return_value = _settings(enabled=0, active=1)

        self.assertFalse(conversation_job_deduplication_enabled())

    @patch("wa_chat_hub.performance_flags.frappe.get_cached_doc")
    def test_activated_setting_can_enable_deduplication(self, get_settings):
        get_settings.return_value = _settings(enabled=1, active=1)

        self.assertTrue(conversation_job_deduplication_enabled())

    @patch(
        "wa_chat_hub.performance_flags.frappe.get_cached_doc",
        side_effect=RuntimeError("schema not ready"),
    )
    def test_schema_error_fails_safe_to_enabled(self, _get_settings):
        self.assertTrue(conversation_job_deduplication_enabled())

    @patch(
        "wa_chat_hub.performance_flags.conversation_job_deduplication_enabled",
        return_value=True,
    )
    def test_enabled_enqueue_options(self, _enabled):
        self.assertEqual(
            conversation_job_enqueue_options("wa_lead_score_CONV-1"),
            {
                "job_id": "wa_lead_score_CONV-1",
                "deduplicate": True,
            },
        )

    @patch(
        "wa_chat_hub.performance_flags.conversation_job_deduplication_enabled",
        return_value=False,
    )
    def test_disabled_enqueue_options(self, _enabled):
        self.assertEqual(
            conversation_job_enqueue_options("wa_lead_score_CONV-1"),
            {},
        )


class TestConversationJobDeduplicationActivationPatch(TestCase):
    def test_patch_preserves_effective_enabled_behavior(self):
        fake_db = SimpleNamespace(
            exists=Mock(return_value=True),
            set_single_value=Mock(),
        )
        fake_meta = _Meta(
            {
                "enable_conversation_job_deduplication",
                "conversation_job_deduplication_setting_active",
            }
        )
        fake_frappe = SimpleNamespace(
            db=fake_db,
            get_meta=Mock(return_value=fake_meta),
            clear_cache=Mock(),
        )

        with patch.object(
            activate_conversation_job_deduplication_setting,
            "frappe",
            fake_frappe,
        ):
            activate_conversation_job_deduplication_setting.execute()

        self.assertEqual(
            fake_db.set_single_value.call_args_list,
            [
                (
                    (
                        "WA Chat Hub Settings",
                        "enable_conversation_job_deduplication",
                        1,
                    ),
                    {},
                ),
                (
                    (
                        "WA Chat Hub Settings",
                        "conversation_job_deduplication_setting_active",
                        1,
                    ),
                    {},
                ),
            ],
        )
        fake_frappe.clear_cache.assert_called_once_with(
            doctype="WA Chat Hub Settings"
        )
