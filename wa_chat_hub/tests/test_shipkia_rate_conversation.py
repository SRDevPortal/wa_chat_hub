from __future__ import annotations

from unittest import TestCase

from wa_chat_hub.ai.lead_scoring import (
    _extract_provider_from_short_answer,
    _extract_shipkia_lead_details,
)
from wa_chat_hub.api.ai_bot import _direct_shipkia_eligibility_decision, _enforce_shipkia_question_limit
from wa_chat_hub.conversation_state import (
    DECISION_OVERWEIGHT_SPLIT_ACCEPTED,
    DECISION_RATE_QUOTE_SENT,
)
from wa_chat_hub.shipkia_rate_card import build_rate_reply_decision


def inbound(body: str) -> dict:
    return {"direction": "Inbound", "body": body}


def outbound(body: str, code: str, *, status: str = "Sent") -> dict:
    return {
        "direction": "Outbound",
        "body": body,
        "delivery_status": status,
        "reply_metadata": {"decision_code": code},
    }


class TestShipKiaRateConversation(TestCase):
    def test_multiline_qualification_list_keeps_only_one_ask(self):
        reply = _enforce_shipkia_question_limit(
            "Please share:\n- Business/store name\n- Monthly shipments\n- Current shipping aggregator\n- Pickup city"
        )
        self.assertIn("Business/store name", reply)
        self.assertNotIn("Monthly shipments", reply)
        self.assertNotIn("shipping aggregator", reply)
        self.assertNotIn("Pickup city", reply)

    def test_broad_rate_intent_asks_only_pickup(self):
        decision = build_rate_reply_decision("I want to know about rates")
        self.assertEqual(decision.decision_code, "shipping_rate.pickup_requested")
        self.assertIn("pickup", decision.text.lower())
        self.assertNotIn("monthly", decision.text.lower())

    def test_harsh_route_and_gr_weight_gets_quote(self):
        history = [
            inbound("I want to know about rates"),
            outbound("Starting rate check karne ke liye pickup city share kar dijiye.", "shipping_rate.pickup_requested"),
        ]
        decision = build_rate_reply_decision("Delhi to banglore 500 gr", history)
        self.assertEqual(decision.decision_code, DECISION_RATE_QUOTE_SENT)
        self.assertIn("500g", decision.text)
        self.assertIn("Rs. 31.2", decision.text)

    def test_rate_details_can_be_collected_one_message_at_a_time(self):
        history = [
            inbound("rates bata do"),
            outbound("Pickup?", "shipping_rate.pickup_requested"),
            inbound("Delhi"),
            outbound("Delivery?", "shipping_rate.delivery_requested"),
            inbound("Bangalore"),
            outbound("Weight?", "shipping_rate.weight_requested"),
        ]
        decision = build_rate_reply_decision("500 gm", history)
        self.assertEqual(decision.decision_code, DECISION_RATE_QUOTE_SENT)
        self.assertIn("Delhi se Bangalore", decision.text)

    def test_failed_outbound_prompt_does_not_advance_state(self):
        history = [
            inbound("rates bata do"),
            outbound("Pickup?", "shipping_rate.pickup_requested", status="Failed"),
        ]
        decision = build_rate_reply_decision("Delhi", history)
        self.assertEqual(decision.decision_code, "shipping_rate.pickup_requested")

    def test_rate_question_is_not_saved_as_aggregator(self):
        self.assertEqual(_extract_provider_from_short_answer("Rates Bata do mujhe"), (None, None, None))

    def test_rate_builder_never_quotes_above_configured_limit(self):
        decision = build_rate_reply_decision("flat rate card for 16 kg", max_package_weight_kg=15)
        self.assertEqual(decision.decision_code, "")
        self.assertEqual(decision.text, "")

    def test_rate_shared_requires_successful_semantic_quote(self):
        phrase_only = _extract_shipkia_lead_details([outbound("ShipKia rates are good", "")])
        failed_quote = _extract_shipkia_lead_details(
            [outbound("Rs. 31.2", DECISION_RATE_QUOTE_SENT, status="Failed")]
        )
        sent_quote = _extract_shipkia_lead_details([outbound("Rs. 31.2", DECISION_RATE_QUOTE_SENT)])
        self.assertFalse(phrase_only.rate_shared)
        self.assertFalse(failed_quote.rate_shared)
        self.assertTrue(sent_quote.rate_shared)

    def test_split_acceptance_uses_new_per_package_weight_for_quote(self):
        split_prompt = _direct_shipkia_eligibility_decision("Delhi to Bangalore 16 kg rate")
        split_history = [
            inbound("Delhi to Bangalore 16 kg rate"),
            outbound(split_prompt.text, split_prompt.decision_code),
        ]
        accepted = _direct_shipkia_eligibility_decision("yes", split_history)
        history = [
            *split_history,
            inbound("yes"),
            outbound(accepted.text, DECISION_OVERWEIGHT_SPLIT_ACCEPTED),
        ]
        quote = build_rate_reply_decision("10 kg", history, max_package_weight_kg=15)
        self.assertEqual(quote.decision_code, DECISION_RATE_QUOTE_SENT)
        self.assertEqual(quote.quote["weight_grams"], 10000)
