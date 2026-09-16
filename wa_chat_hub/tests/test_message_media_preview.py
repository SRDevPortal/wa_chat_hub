from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.api.chat import _attach_message_media_proxy_urls, get_message_media


class TestMessageMediaPreview(TestCase):
    def test_local_uploaded_image_does_not_use_remote_proxy(self):
        for prefix in ("/files/", "/private/files/"):
            row = {"name": "MSG", "content_type": "Image", "media_url": prefix + "photo.png"}
            _attach_message_media_proxy_urls([row])
            self.assertNotIn("media_proxy_url", row)

    def test_local_attachment_takes_precedence_over_remote_original(self):
        row = {
            "name": "MSG", "content_type": "Image", "media_url": "https://example.com/photo.png",
            "attachment_url": "/files/photo.png", "media_proxy_url": "/stale-proxy",
        }
        _attach_message_media_proxy_urls([row])
        self.assertNotIn("media_proxy_url", row)

    def test_incoming_remote_image_keeps_proxy(self):
        row = {"name": "MSG", "content_type": "Image", "media_url": "https://example.com/photo.png"}
        _attach_message_media_proxy_urls([row])
        self.assertTrue(row["media_proxy_url"].endswith("?message=MSG"))

    def test_existing_image_template_renders_as_image_without_changing_stored_type(self):
        row = {"name": "MSG", "content_type": "Template", "media_url": "https://example.com/photo.jpg?sig=x"}
        _attach_message_media_proxy_urls([row])
        self.assertEqual(row["content_type"], "Template")
        self.assertEqual(row["media_content_type"], "Image")
        self.assertIn("media_proxy_url", row)

    def test_header_format_supports_media_urls_without_extensions(self):
        row = {
            "name": "MSG", "content_type": "Template", "media_url": "https://example.com/media/123",
            "raw_transport_payload": {"header_format": "IMAGE"},
        }
        _attach_message_media_proxy_urls([row])
        self.assertEqual(row["media_content_type"], "Image")

    def test_older_template_recovers_header_from_sent_payload(self):
        row = {
            "name": "MSG", "content_type": "Template",
            "raw_transport_payload": '{"payload":{"template":{"headerValues":["https://example.com/a.png"]}}}',
        }
        _attach_message_media_proxy_urls([row])
        self.assertEqual(row["media_url"], "https://example.com/a.png")
        self.assertEqual(row["media_content_type"], "Image")

    def test_text_template_does_not_gain_media(self):
        for raw in (
            '{}', 'not json', '[]',
            '{"payload":{"template":{"headerValues":["Patient"]}}}',
            '{"payload":{"template":{"headerValues":["https://example.com"]}}}',
        ):
            row = {"name": "MSG", "content_type": "Template", "raw_transport_payload": raw}
            _attach_message_media_proxy_urls([row])
            self.assertNotIn("media_content_type", row)
            self.assertNotIn("media_proxy_url", row)

    def test_template_preview_requires_conversation_access(self):
        row = frappe._dict(name="MSG", conversation="CONV", content_type="Template")
        with patch("wa_chat_hub.api.chat.frappe.db.get_value", return_value=row), patch(
            "wa_chat_hub.api.chat.ensure_can_read_conversation", side_effect=PermissionError
        ), patch("wa_chat_hub.api.chat.requests.get") as get:
            with self.assertRaises(PermissionError):
                get_message_media("MSG")
        get.assert_not_called()
