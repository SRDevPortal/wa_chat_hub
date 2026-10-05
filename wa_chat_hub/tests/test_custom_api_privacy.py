import unittest
from unittest.mock import patch

import frappe

from wa_chat_hub import number_privacy
from wa_chat_hub.api import contact_sync, diagnostics, errors, runtime


class CustomAPIPrivacyTests(unittest.TestCase):
    phone = "9876501234"

    def test_provider_and_mcp_key_variants_are_masked(self):
        raw = {
            "phoneNumber": self.phone,
            "channel_phone_number": self.phone,
            "normalized_phone": self.phone,
            "short_reason": f"Provider rejected {self.phone}",
            "provider_response": {"customer": {"phoneNumber": self.phone}},
        }

        result = number_privacy.project(raw)

        self.assertNotIn(self.phone, frappe.as_json(result))
        self.assertNotIn("provider_response", result)
        self.assertEqual(raw["phoneNumber"], self.phone)

    def test_mcp_browser_response_masks_configured_phone_fields(self):
        raw = {
            "success": True,
            "result": {
                "mobile_no": self.phone,
                "phoneNumber": self.phone,
                "content": f"Call {self.phone}",
            },
        }
        with patch.object(runtime.frappe.local, "form_dict", frappe._dict()),              patch.object(runtime.frappe, "request", None),              patch("wa_chat_hub.mcp.invoke_mcp_tool", return_value=raw),              patch.object(number_privacy, "restricted", return_value=True):
            result = runtime.call_mcp_tool()

        self.assertNotIn(self.phone, frappe.as_json(result))
        self.assertEqual(raw["result"]["mobile_no"], self.phone)

    def test_contact_sync_drops_provider_payload_for_restricted_user(self):
        raw = {
            "success": True,
            "contact": self.phone,
            "provider_response": {"phoneNumber": self.phone},
        }
        with patch.object(contact_sync, "push_contact_to_interakt", return_value=raw),              patch.object(number_privacy, "restricted", return_value=True):
            result = contact_sync.push_chat_contact_to_interakt(self.phone, "ACCOUNT-1")

        self.assertNotIn(self.phone, frappe.as_json(result))
        self.assertNotIn("provider_response", result)
        self.assertEqual(raw["contact"], self.phone)

    def test_error_dashboard_masks_phone_and_reason(self):
        rows = [{
            "name": "ERR-1",
            "creation": "2026-10-05 10:00:00",
            "method": "Interakt Message API Failure",
            "error": f"ValidationError: recipient {self.phone}",
        }]
        with patch.object(errors, "_ensure_error_access"),              patch.object(errors.frappe, "get_all", return_value=rows),              patch.object(number_privacy, "restricted", return_value=True):
            result = errors.get_error_logs()

        event = result["result"]["events"][0]
        self.assertNotIn(self.phone, frappe.as_json(event))
        self.assertNotEqual(event["phone"], self.phone)

    def test_diagnostic_message_body_is_masked(self):
        rows = [frappe._dict(
            name="MSG-1",
            creation="2026-10-05 10:00:00",
            conversation="CONV-1",
            content_type="Image",
            body=f"Call {self.phone}",
            media_url="",
            attachment_file="",
            delivery_status="Received",
            channel_message_id="provider-1",
        )]
        with patch.object(diagnostics, "_ensure_diagnostic_access"),              patch.object(diagnostics.frappe, "get_all", return_value=rows),              patch.object(number_privacy, "restricted", return_value=True):
            result = diagnostics.get_latest_inbound_media()

        self.assertNotIn(self.phone, frappe.as_json(result))
        self.assertEqual(rows[0].body, f"Call {self.phone}")
