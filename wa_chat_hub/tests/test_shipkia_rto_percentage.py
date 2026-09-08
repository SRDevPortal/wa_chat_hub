from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.api.ai_bot import _direct_shipkia_sales_reply, _shipkia_details_with_current


class TestShipKiaRtoPercentage(TestCase):
    def test_hinglish_rto_percentage_reply_advances_flow(self):
        history = [
            {"direction": "Inbound", "body": "Shipkia ndr resolve krskta h kya"},
            {"direction": "Outbound", "body": "Aapka approx RTO percentage kitna chal raha hai?"},
        ]

        details = _shipkia_details_with_current(history, "Abhi to 60 per chl rha h")
        reply = _direct_shipkia_sales_reply("Abhi to 60 per chl rha h", history)

        self.assertEqual(details.rto_percentage, 60)
        self.assertIn("60% RTO noted", reply)
        self.assertIn("monthly shipments", reply)
        self.assertNotIn("RTO percentage kitna", reply)

    def test_repeated_hinglish_rto_percentage_reply_is_understood(self):
        history = [
            {"direction": "Inbound", "body": "Shipkia ndr resolve krskta h kya"},
            {"direction": "Outbound", "body": "Aapka approx RTO percentage kitna chal raha hai?"},
            {"direction": "Inbound", "body": "Abhi to 60 per chl rha h"},
            {"direction": "Outbound", "body": "Aapka approx RTO percentage kitna chal raha hai?"},
        ]

        details = _shipkia_details_with_current(history, "Btaya to 60 per")

        self.assertEqual(details.rto_percentage, 60)

    def test_continue_nudge_uses_saved_lead_rto_and_advances_flow(self):
        history = [
            {"direction": "Inbound", "body": "Shipkia ndr resolve krskta h kya"},
            {"direction": "Outbound", "body": "Aapka approx RTO percentage kitna chal raha hai?"},
        ]

        def fake_get_value(doctype, name, fieldname, **kwargs):
            if doctype == "Chat Conversation" and fieldname == "contact":
                return "919084553059"
            if doctype == "Chat Contact":
                return {
                    "linked_lead": "CRM-LEAD-2026-00014",
                    "source_doctype": "Lead",
                    "source_name": "CRM-LEAD-2026-00014",
                    "phone_number": "919084553059",
                }
            if doctype == "Lead" and fieldname == "shipkia_rto_percentage":
                return 60
            return None

        with (
            patch("wa_chat_hub.api.ai_bot.safe_ai_get_value", side_effect=fake_get_value),
            patch("wa_chat_hub.api.ai_bot.safe_ai_exists", return_value=True),
            patch("wa_chat_hub.api.ai_bot.frappe.db.has_column", return_value=True),
        ):
            reply = _direct_shipkia_sales_reply("Bolo", history, conversation="35")

        self.assertIn("monthly shipments", reply)
        self.assertNotIn("RTO percentage kitna", reply)

    def test_continue_nudge_after_business_type_question_is_not_saved_as_business_type(self):
        history = [
            {"direction": "Inbound", "body": "Shipkia ndr resolve krskta h kya"},
            {"direction": "Outbound", "body": "Aapka approx RTO percentage kitna chal raha hai?"},
            {"direction": "Inbound", "body": "Abhi to 60 per chl rha h"},
            {
                "direction": "Outbound",
                "body": "Aapka business type kya hai? B2C, D2C, wholesale, retail, manufacturing, reseller ya other - jo applicable ho bata dijiye.",
            },
        ]

        def fake_get_value(doctype, name, fieldname, **kwargs):
            if doctype == "Chat Conversation" and fieldname == "contact":
                return "919084553059"
            if doctype == "Chat Contact":
                return {
                    "linked_lead": "CRM-LEAD-2026-00014",
                    "source_doctype": "Lead",
                    "source_name": "CRM-LEAD-2026-00014",
                    "phone_number": "919084553059",
                }
            if doctype == "Lead" and fieldname == "shipkia_rto_percentage":
                return 60
            return None

        with (
            patch("wa_chat_hub.api.ai_bot.safe_ai_get_value", side_effect=fake_get_value),
            patch("wa_chat_hub.api.ai_bot.safe_ai_exists", return_value=True),
            patch("wa_chat_hub.api.ai_bot.frappe.db.has_column", return_value=True),
        ):
            reply = _direct_shipkia_sales_reply("Bolo", history, conversation="35")

        self.assertIn("monthly shipments", reply)
        self.assertNotIn("business/store name", reply)
        self.assertNotIn("RTO percentage kitna", reply)
