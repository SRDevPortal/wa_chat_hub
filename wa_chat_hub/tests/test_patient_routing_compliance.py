from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from wa_chat_hub.interakt.contact_sync import push_contact_to_interakt
from wa_chat_hub.patches.v1_0.add_pipeline_map_patient_routing_index import execute


class TestPatientRoutingCompliance(TestCase):
    def test_commits_profile_before_interakt_request(self):
        contact = frappe._dict(
            name="CONTACT-001",
            phone_number="+919999999999",
            display_name="Patient",
            source_doctype="Patient",
            source_name="PAT-001",
        )
        account = frappe._dict(interakt_default_country_code="+91")
        patient = frappe._dict(doctype="Patient", name="PAT-001", patient_name="Patient")
        frappe_mock = MagicMock()

        with (
            patch("wa_chat_hub.interakt.contact_sync.safe_ai_exists", return_value=True),
            patch(
                "wa_chat_hub.interakt.contact_sync.safe_ai_get_doc",
                side_effect=[contact, account],
            ),
            patch(
                "wa_chat_hub.interakt.contact_sync._split_interakt_phone",
                return_value=("+91", "9999999999"),
            ),
            patch(
                "wa_chat_hub.interakt.contact_sync.get_or_create_contact_channel_profile",
                return_value="PROFILE-001",
            ),
            patch("wa_chat_hub.interakt.contact_sync.frappe", frappe_mock),
            patch("wa_chat_hub.interakt.contact_sync.safe_ai_set_value"),
            patch("wa_chat_hub.interakt.contact_sync.extract_interakt_user_id", return_value="user-1"),
            patch("wa_chat_hub.interakt.contact_sync.now_datetime", return_value="2026-08-12 12:00:00"),
            patch("wa_chat_hub.interakt.contact_sync.track_user") as track_user,
        ):
            commit = frappe_mock.db.commit
            track_user.side_effect = lambda *_args, **_kwargs: (
                {"ok": True} if commit.called else self.fail("Interakt called before commit")
            )
            result = push_contact_to_interakt(
                "ACCOUNT-001",
                "CONTACT-001",
                reference_doc=patient,
                pipeline_map_row={"sr_medical_department": "Cardiology"},
            )

        self.assertTrue(result["success"])
        commit.assert_called_once_with()
        track_user.assert_called_once()

    @patch("wa_chat_hub.patches.v1_0.add_pipeline_map_patient_routing_index.frappe")
    def test_index_patch_uses_online_ddl(self, frappe_mock):
        frappe_mock.db.sql.side_effect = [[(1,)], [], None]
        frappe_mock.db.has_column.return_value = True

        execute()

        ddl = frappe_mock.db.sql.call_args_list[-1].args[0]
        self.assertIn("ALGORITHM=INPLACE", ddl)
        self.assertIn("LOCK=NONE", ddl)

    @patch("wa_chat_hub.patches.v1_0.add_pipeline_map_patient_routing_index.frappe")
    def test_index_patch_skips_existing_index(self, frappe_mock):
        frappe_mock.db.sql.side_effect = [[(1,)], [(1,)]]

        execute()

        self.assertEqual(frappe_mock.db.sql.call_count, 2)
        frappe_mock.db.has_column.assert_not_called()
