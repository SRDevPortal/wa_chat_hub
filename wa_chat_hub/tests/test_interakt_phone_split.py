from unittest import TestCase

from wa_chat_hub.connector.interakt.adapter import split_interakt_phone
from wa_chat_hub.channel_resolver import _split_interakt_phone


class InteraktPhoneTests(TestCase):
    def test_country_codes_use_numbering_plan_in_both_senders(self):
        cases = [
            ("+971 50 123 4567", "+91", ("+971", "501234567")),
            ("971501234567", "+91", ("+971", "501234567")),
            ("00971501234567", "+91", ("+971", "501234567")),
            ("+1 415 555 0123", "+91", ("+1", "4155550123")),
            ("+44 7700 900123", "+91", ("+44", "7700900123")),
            ("9876543210", "+91", ("+91", "9876543210")),
            ("919876543210", "+91", ("+91", "9876543210")),
            ("09876543210", "+91", ("+91", "9876543210")),
            ("9123456789", "+91", ("+91", "9123456789")),
            ("0501234567", "+971", ("+971", "501234567")),
            ("+39 02 12345678", "+91", ("+39", "0212345678")),
        ]
        for split in (split_interakt_phone, _split_interakt_phone):
            for phone, default, expected in cases:
                with self.subTest(phone=phone, split=split.__name__):
                    self.assertEqual(split(phone, default), expected)

    def test_invalid_numbers_fail_before_provider_request(self):
        for phone, default in [("+999123456789", "+91"), ("123", "+91"), ("+14155550123 ext 9", "+91"), ("501234567", "+97")]:
            with self.subTest(phone=phone), self.assertRaises(ValueError):
                split_interakt_phone(phone, default)
