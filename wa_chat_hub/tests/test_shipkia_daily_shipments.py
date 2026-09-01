from __future__ import annotations

from unittest import TestCase

from wa_chat_hub.ai.lead_scoring import _extract_shipkia_lead_details
from wa_chat_hub.api.ai_bot import _direct_shipkia_sales_reply


class TestShipKiaDailyShipments(TestCase):
    def test_daily_shipments_are_converted_to_monthly_and_not_provider(self):
        details = _extract_shipkia_lead_details(
            [
                {"direction": "Outbound", "body": "Aap approx monthly shipments kitne karte hain?"},
                {"direction": "Inbound", "body": "20 shipments per day"},
                {
                    "direction": "Outbound",
                    "body": "Aap currently kaunsa shipping aggregator use kar rahe hain?",
                },
                {"direction": "Inbound", "body": "20 daily shipments h not monthly"},
            ]
        )

        self.assertEqual(details.monthly_shipments, 600)
        self.assertIsNone(details.aggregator_status)
        self.assertIsNone(details.aggregator_name)

    def test_harsh_correction_asks_aggregator_instead_of_onboarding(self):
        history = [
            {"direction": "Inbound", "body": "Hii"},
            {"direction": "Outbound", "body": "Hi, welcome to ShipKia. Kaise help kar sakta hoon?"},
            {"direction": "Inbound", "body": "I want to know about shipkia"},
            {"direction": "Outbound", "body": "ShipKia se aap... Aapka main shipping challenge kya hai?"},
            {"direction": "Inbound", "body": "Slow delivery"},
            {"direction": "Outbound", "body": "Aapka business type kya hai?"},
            {"direction": "Inbound", "body": "D2C"},
            {"direction": "Outbound", "body": "Aap business/store name share kar dijiye."},
            {"direction": "Inbound", "body": "Harsh enterprise"},
            {"direction": "Outbound", "body": "Aap approx monthly shipments kitne karte hain?"},
            {"direction": "Inbound", "body": "20 shipments per day"},
            {"direction": "Outbound", "body": "Aap currently kaunsa shipping aggregator use kar rahe hain?"},
        ]

        reply = _direct_shipkia_sales_reply("20 daily shipments h not monthly", history)

        self.assertIn("600 monthly shipments", reply)
        self.assertIn("shipping aggregator", reply)
        self.assertNotIn("onboarding link", reply.lower())

