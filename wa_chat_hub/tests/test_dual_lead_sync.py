from __future__ import annotations

from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from wa_chat_hub.ai.lead_scoring import _add_lead_update, _get_conversation_linked_leads
from wa_chat_hub.services import _ensure_inbound_lead_pair


class TestInboundDualLeadPair(TestCase):
    def test_creates_missing_lead_and_crm_lead(self):
        with (
            patch("wa_chat_hub.services.safe_ai_exists", return_value=True),
            patch("wa_chat_hub.services._find_by_phone", return_value=None),
            patch("wa_chat_hub.services._find_primary_crm_lead_by_phone", return_value=None),
            patch(
                "wa_chat_hub.services._create_lead_for_inbound",
                side_effect=["LEAD-NEW", "CRM-LEAD-NEW"],
            ) as create_lead,
        ):
            result = _ensure_inbound_lead_pair(
                phone_number="919999999999",
                display_name="ShipKia Customer",
                channel_account="Shipkia Test",
            )

        self.assertEqual(result, {"Lead": "LEAD-NEW", "CRM Lead": "CRM-LEAD-NEW"})
        self.assertEqual(
            [call.kwargs["doctype"] for call in create_lead.call_args_list],
            ["Lead", "CRM Lead"],
        )

    def test_reuses_existing_lead_and_only_creates_crm_counterpart(self):
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

        self.assertEqual(result, {"Lead": "LEAD-EXISTING", "CRM Lead": "CRM-LEAD-NEW"})
        create_lead.assert_called_once_with(
            doctype="CRM Lead",
            phone_number="919999999999",
            display_name="ShipKia Customer",
            channel_account="Shipkia Test",
        )


class TestLinkedLeadCollection(TestCase):
    def test_collects_crm_and_erpnext_lead_without_duplicates(self):
        conversation = frappe._dict(
            linked_reference_doctype="CRM Lead",
            linked_reference_name="CRM-LEAD-001",
            linked_crm_lead="CRM-LEAD-001",
            contact="CHAT-CONTACT-001",
        )
        contact = frappe._dict(
            source_doctype="CRM Lead",
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
            [("CRM Lead", "CRM-LEAD-001"), ("Lead", "LEAD-001")],
        )

    def test_converts_weight_with_units_for_numeric_crm_field(self):
        field = frappe._dict(fieldtype="Float")
        meta = MagicMock()
        meta.has_field.return_value = True
        meta.get_field.return_value = field
        updates = {}

        _add_lead_update(meta, updates, "shipkia_average_weight", "500 g")

        self.assertEqual(updates["shipkia_average_weight"], 500.0)
