from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.connector.interakt.adapter import InteraktAdapter, split_interakt_phone
from wa_chat_hub.interakt.templates_api import _normalize_templates, resolve_approved_template


SAMPLE_MEDIA_URL = "https://interakt.example/message_template_sample/header.png?se=2031-05-20"


class TestInteraktTemplateHeaderMedia(TestCase):
    def test_normalization_preserves_interakt_header_image(self):
        templates = _normalize_templates(
            [
                {
                    "element_name": "welcome_image",
                    "approval_status": "APPROVED",
                    "language_code": "en",
                    "header_format": "IMAGE",
                    "header_handle_file_url": SAMPLE_MEDIA_URL,
                    "header_handle_file_name": "header.png",
                    "body": "Welcome",
                }
            ]
        )

        self.assertEqual(templates[0]["header_format"], "IMAGE")
        self.assertTrue(templates[0]["requires_header_media"])
        self.assertEqual(templates[0]["header_media_url"], SAMPLE_MEDIA_URL)

    def test_meta_component_example_header_handle_is_supported(self):
        templates = _normalize_templates(
            [
                {
                    "name": "component_image",
                    "status": "approved",
                    "language": "en",
                    "components": [
                        {
                            "type": "HEADER",
                            "format": "IMAGE",
                            "example": {"header_handle": [SAMPLE_MEDIA_URL]},
                        },
                        {"type": "BODY", "text": "Welcome"},
                    ],
                }
            ]
        )

        self.assertEqual(templates[0]["header_media_url"], SAMPLE_MEDIA_URL)

    @patch("wa_chat_hub.interakt.templates_api.fetch_approved_templates")
    def test_resolver_uses_stored_header_image_as_header_value(self, fetch_templates):
        fetch_templates.return_value = [
            {
                "name": "welcome_image",
                "language_code": "en",
                "body_variable_count": 0,
                "header_format": "IMAGE",
                "header_media_url": SAMPLE_MEDIA_URL,
            }
        ]

        resolved = resolve_approved_template(
            "ACCOUNT-1",
            {"template_name": "welcome_image", "language_code": "en"},
        )

        self.assertEqual(resolved["header_values"], [SAMPLE_MEDIA_URL])

    def test_adapter_places_media_url_in_template_header_values(self):
        payload = InteraktAdapter().build_outbound_payload(
            {
                "phone_number": "919999999999",
                "default_country_code": "+91",
                "content_type": "Template",
                "template": {
                    "name": "welcome_image",
                    "languageCode": "en",
                    "headerValues": [SAMPLE_MEDIA_URL],
                },
            }
        )

        self.assertEqual(payload["template"]["headerValues"], [SAMPLE_MEDIA_URL])

    def test_split_interakt_phone_infers_qatar_country_code(self):
        self.assertEqual(split_interakt_phone("97474731407", "+91"), ("+974", "74731407"))
        self.assertEqual(split_interakt_phone("+97474731407", "+91"), ("+974", "74731407"))

    def test_split_interakt_phone_preserves_india_default(self):
        self.assertEqual(split_interakt_phone("919999999999", "+91"), ("+91", "9999999999"))
        self.assertEqual(split_interakt_phone("9999999999", "+91"), ("+91", "9999999999"))
