from __future__ import annotations

from unittest import TestCase

from wa_chat_hub.api.ai_bot import (
    _direct_shipkia_greeting_reply,
    _looks_like_answer_to_recent_sales_question,
)


class TestShipKiaGreetingReset(TestCase):
    def test_plain_hello_after_old_sales_question_is_greeting_not_answer(self):
        history = [
            {"direction": "Inbound", "body": "Onboarding krwa do mujhe"},
            {
                "direction": "Outbound",
                "body": "Bilkul. ShipKia onboarding link: https://auth.shipkia.com/signup",
            },
            {"direction": "Inbound", "body": "Thank you"},
            {"direction": "Outbound", "body": "Aapka business type kya hai?"},
        ]

        self.assertEqual(
            _direct_shipkia_greeting_reply("Hello", history),
            "Hi, welcome to ShipKia. Kaise help kar sakta hoon?",
        )
        self.assertFalse(_looks_like_answer_to_recent_sales_question(history, "Hello"))

