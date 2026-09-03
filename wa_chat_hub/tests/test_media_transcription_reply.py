from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.ai.media_transcription import build_transcript_context_for_chat


class TestMediaTranscriptionReply(TestCase):
    @patch("wa_chat_hub.ai.media_transcription.policy_section", return_value={})
    @patch("wa_chat_hub.ai.media_transcription.transcribe_media")
    def test_precomputed_transcript_is_customer_turn_without_second_api_call(
        self,
        transcribe_media,
        _policy_section,
    ):
        context = build_transcript_context_for_chat(
            "https://example.test/voice.ogg",
            "Audio",
            "[Audio message received]",
            transcript="Please book an appointment tomorrow",
        )

        transcribe_media.assert_not_called()
        self.assertIn("Please book an appointment tomorrow", context)
        self.assertIn("respond directly to the customer", context)
        self.assertNotIn("authorized staff review", context)

    @patch(
        "wa_chat_hub.ai.media_transcription.policy_section",
        return_value={"transcript_reply_prompt": "CUSTOM DIRECT REPLY RULE"},
    )
    def test_channel_policy_can_override_customer_reply_instruction(self, _policy_section):
        context = build_transcript_context_for_chat(
            "https://example.test/voice.ogg",
            "Audio",
            transcript="What time is my appointment?",
        )

        self.assertIn("CUSTOM DIRECT REPLY RULE", context)
