import json
import unittest
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request, Response
from wa_chat_hub import rest_privacy


class RestPrivacyTests(unittest.TestCase):
    def run_projection(self, path, data, restricted=True, method="GET", status=200):
        request = Request(EnvironBuilder(path=path, method=method).get_environ())
        response = Response(json.dumps({"data": data}), status=status, mimetype="application/json")
        with patch.object(rest_privacy, "restricted", return_value=restricted), \
             patch.object(frappe.local, "form_dict", frappe._dict()):
            rest_privacy.protect_conversation_read(request, response)
        return response

    def test_list_and_detail_hide_original_and_payload(self):
        raw = {"name": "1042", "contact": "9876501234", "last_message_preview": "Call 9876501234",
               "raw_payload": {"phone": "9876501234"}}
        for prefix in ("/api/resource/", "/api/v1/resource/", "/api/v2/document/"):
            for suffix, data in (("", [raw]), ("/1042", raw)):
                result = self.run_projection(prefix + "Chat%20Conversation" + suffix, data)
                self.assertNotIn("9876501234", result.get_data(as_text=True))
                self.assertIn("1042", result.get_data(as_text=True))
                self.assertEqual(result.headers["Cache-Control"], "no-store")
        self.assertEqual(raw["contact"], "9876501234")

    def test_full_visibility_and_unrelated_routes_unchanged(self):
        raw = {"contact": "9876501234"}
        for path, restricted, method, status in (
            ("/api/resource/Chat%20Conversation/1042", False, "GET", 200),
            ("/api/resource/Chat%20Conversation/1042", True, "PUT", 200),
            ("/api/resource/Customer/1042", True, "GET", 200),
            ("/api/method/wa_chat_hub.api.webhook.receive", True, "GET", 200),
            ("/api/resource/Chat%20Conversation/1042", True, "GET", 403),
        ):
            result = self.run_projection(path, raw, restricted, method, status)
            self.assertEqual(result.get_json()["data"], raw)
            self.assertEqual(result.status_code, status)

    def test_projection_failure_does_not_return_original(self):
        with patch.object(rest_privacy, "project_read", side_effect=ValueError):
            result = self.run_projection("/api/resource/Chat%20Conversation/1042", {"contact": "9876501234"})
        self.assertEqual(result.status_code, 500)
        self.assertNotIn("9876501234", result.get_data(as_text=True))

    def test_v2_copy_masks_original_contact(self):
        result = self.run_projection("/api/v2/document/Chat%20Conversation/1042/copy",
                                     {"contact": "9876501234", "status": "Open"})
        self.assertNotIn("9876501234", result.get_data(as_text=True))
        self.assertEqual(result.get_json()["data"]["status"], "Open")

    def test_v2_methods_and_writes_unchanged(self):
        raw = {"contact": "9876501234"}
        for path, method in (
            ("/api/v2/document/Chat%20Conversation/1042", "PATCH"),
            ("/api/v2/document/Chat%20Conversation", "POST"),
            ("/api/v2/document/Chat%20Conversation/1042/method/get_title", "GET"),
        ):
            self.assertEqual(self.run_projection(path, raw, method=method).get_json()["data"], raw)
