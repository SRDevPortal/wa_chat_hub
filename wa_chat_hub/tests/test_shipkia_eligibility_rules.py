from __future__ import annotations

from unittest import TestCase

import frappe

from wa_chat_hub.ai.lead_scoring import (
    ShipKiaLeadDetails,
    _extract_shipkia_lead_details,
    _policy_rule_score,
    _qualification_status,
    detect_shipkia_service_restriction,
)
from wa_chat_hub.api.ai_bot import (
    _direct_shipkia_eligibility_decision,
    _direct_shipkia_eligibility_reply,
    _shipkia_next_sales_question,
)


SCORING_POLICY = {
    "base_score": 20,
    "inbound_message_cap": 30,
    "inbound_message_weight": 3,
    "unread_cap": 15,
    "unread_weight": 2,
    "priority_weights": {},
    "hot_terms": [],
    "warm_terms": [],
    "temperature_bands": [
        {"minimum": 70, "label": "Hot"},
        {"minimum": 40, "label": "Warm"},
        {"minimum": 0, "label": "Cold"},
    ],
}


def inbound(body: str) -> list[dict]:
    return [{"direction": "Inbound", "body": body, "creation": "2026-09-07 10:00:00"}]


class TestShipKiaEligibilityReplies(TestCase):
    def test_declines_job_without_sales_question(self):
        reply = _direct_shipkia_eligibility_reply("Do you have any job vacancy? I can send my CV")
        self.assertEqual(reply, "We don't handle job applications through this WhatsApp channel.")
        self.assertNotIn("?", reply)

    def test_declines_b2b_and_international(self):
        self.assertEqual(detect_shipkia_service_restriction("We need B2B shipping"), "b2b")
        self.assertEqual(detect_shipkia_service_restriction("Need international shipping"), "international")

    def test_allows_exactly_fifteen_kg_and_flags_above(self):
        self.assertEqual(detect_shipkia_service_restriction("Each parcel is 15 kg"), "")
        self.assertEqual(detect_shipkia_service_restriction("Each parcel is 15.1 kg"), "overweight")
        self.assertEqual(detect_shipkia_service_restriction("Parcel is 25000 grams"), "overweight")
        self.assertEqual(detect_shipkia_service_restriction("Parcel is more than 15 kg"), "overweight")

    def test_weight_limit_can_be_configured(self):
        self.assertEqual(
            detect_shipkia_service_restriction("Each parcel is 12.1 kg", max_package_weight_kg=12),
            "overweight",
        )
        reply = _direct_shipkia_eligibility_reply("Each parcel is 12.1 kg", max_package_weight_kg=12)
        self.assertIn("12 kg", reply)

    def test_declines_plain_weight_above_limit_after_weight_question(self):
        history = [
            {"direction": "Outbound", "body": "Approx shipment weight kitna rahega?"},
        ]
        reply = _direct_shipkia_eligibility_reply("25", history)
        self.assertIn("split", reply.lower())
        self.assertIn("15 kg", reply)

    def test_overweight_split_yes_asks_weight_per_package(self):
        prompt = _direct_shipkia_eligibility_decision("It is 16 kg")
        history = [
            {
                "direction": "Outbound",
                "body": prompt.text,
                "reply_metadata": {"decision_code": prompt.decision_code},
            }
        ]
        reply = _direct_shipkia_eligibility_decision("haan", history)
        self.assertIn("Har split package", reply.text)
        self.assertEqual(reply.decision_code, "shipping_weight.split_accepted")

    def test_overweight_split_no_declines_with_apology(self):
        prompt = _direct_shipkia_eligibility_decision("It is 16 kg")
        history = [
            {
                "direction": "Outbound",
                "body": prompt.text,
                "reply_metadata": {"decision_code": prompt.decision_code},
            }
        ]
        reply = _direct_shipkia_eligibility_decision("nahi", history)
        self.assertTrue(reply.text.startswith("Sorry"))
        self.assertIn("possible nahi", reply.text)

    def test_mixed_b2b_b2c_continues_only_for_supported_volume(self):
        reply = _direct_shipkia_eligibility_reply("We have both B2B and B2C orders")
        self.assertIn("cannot handle the B2B portion", reply)
        self.assertIn("monthly eligible shipments", reply)


class TestShipKiaShipmentPriority(TestCase):
    def test_monthly_shipments_is_first_qualification_question(self):
        question = _shipkia_next_sales_question(ShipKiaLeadDetails())
        self.assertEqual(question, "Aap approx monthly shipments kitne karte hain?")

    def test_one_hundred_monthly_shipments_gets_maximum_score(self):
        result = _policy_rule_score(
            frappe._dict(unread_count=0, priority="Low"),
            inbound("We do 100 monthly shipments"),
            "English",
            SCORING_POLICY,
        )
        self.assertEqual(result.lead_score, 100)
        self.assertEqual(result.lead_temperature, "Hot")
        self.assertEqual(result.source, "monthly_shipments_maximum")

    def test_ninety_nine_monthly_shipments_does_not_get_maximum(self):
        result = _policy_rule_score(
            frappe._dict(unread_count=0, priority="Low"),
            inbound("We do 99 monthly shipments"),
            "English",
            SCORING_POLICY,
        )
        self.assertLess(result.lead_score, 100)

    def test_unsupported_scope_overrides_high_volume(self):
        result = _policy_rule_score(
            frappe._dict(unread_count=0, priority="Low"),
            inbound("We need international B2B shipping for 1000 monthly shipments"),
            "English",
            SCORING_POLICY,
        )
        self.assertEqual(result.lead_score, 0)
        self.assertEqual(result.lead_temperature, "Cold")
        self.assertTrue(result.source.startswith("disqualified_"))

    def test_weekly_shipments_are_converted_before_scoring(self):
        details = _extract_shipkia_lead_details(inbound("We ship 25 orders per week"))
        self.assertEqual(details.monthly_shipments, 100)
        self.assertEqual(_qualification_status(details), "Qualified")
