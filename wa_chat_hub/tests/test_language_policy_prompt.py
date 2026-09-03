from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.ai.language import append_multilingual_instructions, detect_customer_language


LANGUAGE_POLICY = {
    "default_language": "auto",
    "script_ratio_threshold": 0.15,
    "script_reply_instruction": "Reply in {language} using the same script as the customer.",
    "prompt_policy": (
        "If the message is Urdu or uses Urdu/Arabic script, understand it but reply in natural Hindi "
        "using Devanagari script only. Never reply in Urdu or Arabic script."
    ),
    "script_ranges": [
        {"code": "ur", "label": "Urdu", "start": "0600", "end": "06FF"},
    ],
}


class TestLanguagePolicyPrompt(TestCase):
    def test_detection_remains_policy_driven_without_python_override(self):
        result = detect_customer_language(
            "جی ہاں، ڈاکٹر سے بات کرنی ہے",
            policy=LANGUAGE_POLICY,
        )

        self.assertEqual(result["code"], "ur")
        self.assertEqual(result["label"], "Urdu")

    @patch("wa_chat_hub.ai.language._language_policy", return_value=LANGUAGE_POLICY)
    def test_configured_urdu_to_hindi_rule_reaches_reply_prompt(self, _language_policy):
        prompt = append_multilingual_instructions(
            "BASE SYSTEM PROMPT",
            "جی ہاں، ڈاکٹر سے بات کرنی ہے",
            channel_account="ACCOUNT-1",
        )

        self.assertIn("BASE SYSTEM PROMPT", prompt)
        self.assertIn("reply in natural Hindi", prompt)
        self.assertIn("Never reply in Urdu", prompt)

