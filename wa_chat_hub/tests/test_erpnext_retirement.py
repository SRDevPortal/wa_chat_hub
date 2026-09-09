"""Integration contract on a site without Frappe CRM; all network IO is mocked."""
from contextlib import ExitStack
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace

import frappe

from wa_chat_hub.services import append_message
from wa_chat_hub.security import ensure_default_ai_doctype_permissions


class TestERPNextRetirement(TestCase):
    def setUp(self):
        self.user = frappe.session.user
        frappe.set_user("Administrator")
        self.assertNotIn("crm", frappe.get_installed_apps())
        self.stack = ExitStack()
        self.stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("No real HTTP in tests")))
        self.stack.enter_context(patch("frappe.enqueue"))
        self.stack.enter_context(patch("frappe.db.commit"))
        self.tag = "retirement-" + uuid4().hex[:12]
        self.phone = "447700" + str(int(uuid4().hex[:8], 16)).zfill(10)
        self.account = frappe.get_doc({"doctype": "Chat Channel Account", "name": self.tag,
            "account_name": self.tag, "channel_type": "Interakt", "is_active": 1})
        self.account.db_insert()
        ensure_default_ai_doctype_permissions()

    def tearDown(self):
        frappe.db.rollback()
        self.stack.close()
        frappe.set_user(self.user)

    def inbound(self, ident=None):
        return append_message({"channel_account": self.account.name, "phone_number": self.phone,
            "display_name": "Retirement Test Customer", "direction": "Inbound", "sender_type": "Customer",
            "content_type": "Text", "body": "I ship 500 parcels monthly", "provider_name": "Interakt",
            "channel_message_id": ident or self.tag, "provider_message_id": ident or self.tag})

    def test_inbound_creates_one_lead_and_reuses_it_for_next_message(self):
        before = frappe.db.count("Lead")
        first = self.inbound()
        second = self.inbound(self.tag + "-second")
        self.assertEqual(first["conversation"], second["conversation"])
        self.assertEqual(frappe.db.count("Lead"), before + 1)
        convo = frappe.get_doc("Chat Conversation", first["conversation"])
        self.assertEqual(convo.linked_reference_doctype, "Lead")
        self.assertTrue(convo.linked_lead)
        self.assertEqual(convo.linked_lead, convo.linked_reference_name)
        self.assertEqual(frappe.db.get_value("Chat Contact", first["contact"], "linked_lead"), convo.linked_lead)
        self.assertEqual(frappe.db.get_value("Lead", convo.linked_lead, "status"), "Lead")
        self.assertFalse(frappe.db.exists("DocType", "CRM Lead"))

    def test_existing_lead_is_reused_and_score_updates_it(self):
        from wa_chat_hub.ai.lead_scoring import ScoreResult, sync_to_linked_lead
        lead = frappe.get_doc({"doctype": "Lead", "first_name": "Existing", "mobile_no": self.phone})
        lead.insert(ignore_permissions=True)
        before = frappe.db.count("Lead")
        result = self.inbound()
        convo = frappe.get_doc("Chat Conversation", result["conversation"])
        self.assertEqual(convo.linked_lead, lead.name)
        self.assertEqual(frappe.db.count("Lead"), before)
        sync_to_linked_lead(convo.name, ScoreResult(85, "Hot", "English", "test"))
        self.assertEqual(float(frappe.db.get_value("Lead", lead.name, "lead_score")), 85)

    def test_pending_reply_and_delivery_update_same_message(self):
        from wa_chat_hub.api.runtime import send_pending_reply_to_provider
        from wa_chat_hub.api.webhook import _update_message_status
        incoming = self.inbound()
        outgoing = append_message({"channel_account": self.account.name, "phone_number": self.phone,
            "direction": "Outbound", "sender_type": "Agent", "body": "Test reply", "delivery_status": "Pending"})
        provider_id = "provider-" + self.tag
        with patch("wa_chat_hub.outbound.send_interakt_message", return_value={"sent": True, "delivery_status": "Sent", "provider_message_id": provider_id}) as send:
            result = send_pending_reply_to_provider(outgoing["message"], incoming["conversation"], "Test reply")
            self.assertTrue(result["success"])
            send.assert_called_once()
            send_pending_reply_to_provider(outgoing["message"], incoming["conversation"], "Test reply")
            send.assert_called_once()
        _update_message_status(provider_id, "Read", {})
        self.assertEqual(frappe.db.get_value("Chat Message", outgoing["message"], "delivery_status"), "Read")

    def test_migrated_chat_schema_and_links_are_erpnext_only(self):
        for dt, field in [("Chat Conversation", "linked_lead"), ("WA Lead AI Insight", "lead"), ("WA Lead OCR Result", "lead")]:
            self.assertEqual(frappe.get_meta(dt).get_field(field).options, "Lead")
        self.assertEqual(frappe.db.count("Chat Conversation", {"linked_reference_doctype": "CRM Lead"}), 0)
        missing = frappe.db.sql("""SELECT c.name FROM `tabChat Conversation` c
            LEFT JOIN `tabLead` l ON l.name=c.linked_lead
            WHERE COALESCE(c.linked_lead,'')!='' AND l.name IS NULL""")
        self.assertEqual(missing, ())

    def test_manual_send_api_persists_pending_before_provider_call(self):
        from wa_chat_hub.api.runtime import send_reply
        incoming = self.inbound()
        request = SimpleNamespace(get_json=lambda **kwargs: {"conversation": incoming["conversation"], "body": "Manual test"})
        with patch.object(frappe.local, "request", request, create=True), patch("wa_chat_hub.api.runtime._enqueue_pending_reply_send") as queue:
            response = send_reply.__wrapped__()
        self.assertTrue(response["result"]["queued"])
        self.assertFalse(response["result"]["sent"])
        self.assertEqual(frappe.db.get_value("Chat Message", response["result"]["message"], "delivery_status"), "Pending")
        queue.assert_called_once()

    def test_ai_suggestion_and_auto_reply_use_same_erpnext_conversation(self):
        from wa_chat_hub.api.ai_bot import _deliver_or_draft_ai_reply
        incoming = self.inbound()
        conversation = incoming["conversation"]
        with patch("wa_chat_hub.api.ai_bot.send_outbound_message", return_value={"delivery_status": "Sent", "provider_message_id": self.tag + "-ai"}) as send:
            mode = _deliver_or_draft_ai_reply(conversation, "Suggested answer", SimpleNamespace(autopilot_mode="Suggest Only"))
            self.assertEqual(mode, "draft")
            send.assert_not_called()
            self.assertTrue(frappe.db.exists("Chat AI Suggestion", {"conversation": conversation, "content": "Suggested answer"}))
            mode = _deliver_or_draft_ai_reply(conversation, "Automatic answer", SimpleNamespace(autopilot_mode="Limited Auto Reply"))
            self.assertEqual(mode, "auto_send")
            send.assert_called_once()
        self.assertTrue(frappe.db.exists("Chat Message", {"conversation": conversation, "sender_type": "AI", "delivery_status": "Sent"}))

    def test_repeated_webhook_does_not_create_duplicate_message_or_lead(self):
        from wa_chat_hub.api.webhook import _process_interakt_payload
        event = SimpleNamespace(channel_account=self.account.name, phone_number=self.phone,
            display_name="Replay Test", channel_message_id=self.tag, direction="Inbound",
            sender_type="Customer", content_type="Text", body="Hello")
        payload = {"channel_account": self.account.name, "type": "message_received"}
        with patch("wa_chat_hub.connector.interakt.adapter.InteraktAdapter.normalize_inbound", return_value=event):
            first = _process_interakt_payload(payload)
            second = _process_interakt_payload(payload)
        self.assertTrue(first["success"])
        self.assertEqual(second["message"], "Duplicate message ignored")
        self.assertEqual(frappe.db.count("Chat Message", {"channel_message_id": self.tag}), 1)
