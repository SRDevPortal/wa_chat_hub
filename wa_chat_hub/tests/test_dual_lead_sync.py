from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from wa_chat_hub.ai.lead_scoring import _add_lead_update, _get_conversation_linked_leads
from wa_chat_hub.services import _ensure_inbound_lead_pair


class TestInboundERPNextLead(TestCase):
    def test_creates_only_one_erpnext_lead(self):
        with (
            patch("wa_chat_hub.services.safe_ai_exists", return_value=True),
            patch("wa_chat_hub.services._find_by_phone", return_value=None),
            patch("wa_chat_hub.services._find_primary_crm_lead_by_phone", return_value=None),
            patch(
                "wa_chat_hub.services._create_lead_for_inbound",
                return_value="LEAD-NEW",
            ) as create_lead,
        ):
            result = _ensure_inbound_lead_pair(
                phone_number="919999999999",
                display_name="ShipKia Customer",
                channel_account="Shipkia Test",
            )

        self.assertEqual(result, {"Lead": "LEAD-NEW"})
        create_lead.assert_called_once_with("Lead", "919999999999", "ShipKia Customer", "Shipkia Test")

    def test_reuses_existing_lead_without_creating_counterpart(self):
        with (
            patch("wa_chat_hub.services.safe_ai_exists", return_value=True),
            patch("wa_chat_hub.services._find_primary_crm_lead_by_phone", return_value=None),
            patch(
                "wa_chat_hub.services._create_lead_for_inbound",
                return_value="CRM-LEAD-NEW",
            ) as create_lead,
        ):
            result = _ensure_inbound_lead_pair(
                phone_number="919999999999",
                display_name="ShipKia Customer",
                channel_account="Shipkia Test",
                existing_doctype="Lead",
                existing_name="LEAD-EXISTING",
            )

        self.assertEqual(result, {"Lead": "LEAD-EXISTING"})
        create_lead.assert_not_called()


class TestLinkedLeadCollection(TestCase):
    def test_canonical_conversation_lead_wins_over_stale_contact(self):
        conversation = frappe._dict(
            linked_reference_doctype="Lead",
            linked_reference_name="CRM-LEAD-001",
            linked_lead="CRM-LEAD-001",
            contact="CHAT-CONTACT-001",
        )
        contact = frappe._dict(
            source_doctype="Lead",
            source_name="CRM-LEAD-001",
            linked_lead="LEAD-001",
        )
        with (
            patch("wa_chat_hub.ai.lead_scoring.safe_ai_exists", return_value=True),
            patch("wa_chat_hub.ai.lead_scoring.safe_ai_get_doc", return_value=contact),
            patch(
                "wa_chat_hub.ai.lead_scoring.get_conversation_crm_lead",
                return_value="CRM-LEAD-001",
            ),
        ):
            result = _get_conversation_linked_leads(conversation)

        self.assertEqual(
            result,
            [("Lead", "CRM-LEAD-001")],
        )

    def test_converts_weight_with_units_for_numeric_crm_field(self):
        field = frappe._dict(fieldtype="Float")
        meta = MagicMock()
        meta.has_field.return_value = True
        meta.get_field.return_value = field
        updates = {}

        _add_lead_update(meta, updates, "shipkia_average_weight", "500 g")

        self.assertEqual(updates["shipkia_average_weight"], 500.0)
