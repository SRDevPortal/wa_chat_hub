from __future__ import annotations

from contextlib import nullcontext
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from wa_chat_hub.api.actions import create_crm_lead_from_conversation


class TestCreateCRMLeadAction(TestCase):
    def test_reuses_phone_match_and_links_current_conversation(self):
        convo = frappe._dict(
            name="CONV-001",
            contact="CONTACT-001",
            channel_account="ACCOUNT-001",
            linked_crm_lead=None,
            linked_reference_doctype=None,
            linked_reference_name=None,
        )
        contact = frappe._dict(
            name="CONTACT-001",
            phone_number="+91 99999 99999",
            display_name="Patient",
        )
        frappe_mock = MagicMock()
        frappe_mock.get_doc.side_effect = [convo, contact]
        frappe_mock.get_meta.return_value.has_field.return_value = True

        with (
            patch("wa_chat_hub.api.actions._load_payload", return_value={"conversation": "CONV-001"}),
            patch("wa_chat_hub.api.actions.ensure_can_read_conversation"),
            patch("wa_chat_hub.api.actions.safe_ai_exists", return_value=True),
            patch("wa_chat_hub.api.actions.conversation_update_lock", return_value=nullcontext()),
            patch("wa_chat_hub.api.actions.get_conversation_crm_lead", return_value=None),
            patch("wa_chat_hub.api.actions._find_primary_crm_lead_by_phone", return_value="CRM-001"),
            patch("wa_chat_hub.api.actions._create_lead_for_inbound") as create_lead,
            patch("wa_chat_hub.api.actions.set_conversation_crm_lead") as set_lead,
            patch("wa_chat_hub.api.actions._finalize_crm_lead_after_inbound"),
            patch("wa_chat_hub.api.actions.score_and_sync_conversation"),
            patch("wa_chat_hub.api.actions.sync_to_linked_lead"),
            patch("wa_chat_hub.api.actions.frappe", frappe_mock),
        ):
            result = create_crm_lead_from_conversation.__wrapped__()

        self.assertFalse(result["result"]["created"])
        self.assertEqual(result["result"]["name"], "CRM-001")
        create_lead.assert_not_called()
        set_lead.assert_called_once_with(convo, "CRM-001")
        self.assertEqual(frappe_mock.db.set_value.call_count, 2)

    def test_creates_crm_lead_with_current_channel_account(self):
        convo = frappe._dict(
            name="CONV-001",
            contact="CONTACT-001",
            channel_account="ACCOUNT-001",
        )
        contact = frappe._dict(
            name="CONTACT-001",
            phone_number="919999999999",
            display_name="New Contact",
        )
        frappe_mock = MagicMock()
        frappe_mock.get_doc.side_effect = [convo, contact]
        frappe_mock.get_meta.return_value.has_field.return_value = True

        with (
            patch("wa_chat_hub.api.actions._load_payload", return_value={"conversation": "CONV-001"}),
            patch("wa_chat_hub.api.actions.ensure_can_read_conversation"),
            patch("wa_chat_hub.api.actions.safe_ai_exists", return_value=True),
            patch("wa_chat_hub.api.actions.conversation_update_lock", return_value=nullcontext()),
            patch("wa_chat_hub.api.actions.get_conversation_crm_lead", return_value=None),
            patch("wa_chat_hub.api.actions._find_primary_crm_lead_by_phone", return_value=None),
            patch("wa_chat_hub.api.actions._create_lead_for_inbound", return_value="CRM-NEW") as create_lead,
            patch("wa_chat_hub.api.actions.set_conversation_crm_lead"),
            patch("wa_chat_hub.api.actions._finalize_crm_lead_after_inbound"),
            patch("wa_chat_hub.api.actions.score_and_sync_conversation"),
            patch("wa_chat_hub.api.actions.sync_to_linked_lead"),
            patch("wa_chat_hub.api.actions.frappe", frappe_mock),
        ):
            result = create_crm_lead_from_conversation.__wrapped__()

        self.assertTrue(result["result"]["created"])
        create_lead.assert_called_once_with(
            doctype="CRM Lead",
            phone_number="919999999999",
            display_name="New Contact",
            channel_account="ACCOUNT-001",
        )
