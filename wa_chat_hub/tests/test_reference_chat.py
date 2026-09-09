"""Database navigation tests; fixtures and changes are rolled back, no messages sent."""

from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

import frappe

from wa_chat_hub.api.reference_chat import find_chats
from wa_chat_hub.permissions import can_read_conversation


class TestReferenceChat(TestCase):
    def setUp(self):
        self.previous_user = frappe.session.user
        frappe.set_user("Administrator")
        self.prefix = "chat-link-test-" + uuid4().hex[:10]
        self.phone = "447700" + str(int(uuid4().hex[:8], 16)).zfill(10)
        self.lead = self.insert(
            "Lead",
            name=self.prefix,
            first_name="Chat Test",
            lead_name="Chat Test",
            status="Lead",
            mobile_no="+" + self.phone,
        )
        self.customer = self.insert(
            "Customer",
            name=self.prefix,
            customer_name="Chat Test",
            lead_name=self.lead.name,
        )
        self.contact = self.insert(
            "Chat Contact", name=self.phone, phone_number=self.phone
        )

    def tearDown(self):
        frappe.db.rollback()
        frappe.set_user(self.previous_user)

    def insert(self, doctype, **values):
        doc = frappe.get_doc({"doctype": doctype, **values})
        doc.db_insert()
        return doc

    def conversation(self, doctype="Lead", name=None, status="Open"):
        return self.insert(
            "Chat Conversation",
            contact=self.contact.name,
            channel_account="test-channel",
            linked_reference_doctype=doctype,
            linked_reference_name=name or self.lead.name,
            status=status,
        )

    def names(self, doctype, name):
        return {str(row["name"]) for row in find_chats(doctype, name)["conversations"]}

    def test_lead_and_converted_customer_open_same_chat(self):
        chat = self.conversation()
        before = frappe.db.count("Chat Conversation")
        self.assertEqual(self.names("Lead", self.lead.name), {str(chat.name)})
        self.assertEqual(self.names("Customer", self.customer.name), {str(chat.name)})
        self.assertEqual(frappe.db.count("Chat Conversation"), before)
        self.assertEqual(
            frappe.db.get_value(
                "Chat Conversation", chat.name, "linked_reference_doctype"
            ),
            "Lead",
        )

    def test_customer_link_is_found_from_original_lead(self):
        chat = self.conversation("Customer", self.customer.name)
        self.assertEqual(self.names("Lead", self.lead.name), {str(chat.name)})

    def test_phone_fallback_keeps_closed_history_and_country_code(self):
        chat = self.conversation("Customer", "unrelated", status="Closed")
        self.assertEqual(self.names("Lead", self.lead.name), {str(chat.name)})
        frappe.db.set_value("Lead", self.lead.name, "mobile_no", "91" + self.phone[2:])
        self.assertEqual(self.names("Lead", self.lead.name), set())

    def test_multiple_matches_and_direct_link_priority(self):
        first = self.conversation()
        second = self.conversation(status="Closed")
        self.conversation("Customer", "unrelated")
        self.assertEqual(
            self.names("Lead", self.lead.name), {str(first.name), str(second.name)}
        )

    def test_missing_chat_does_not_create_one(self):
        before = frappe.db.count("Chat Conversation")
        self.assertEqual(self.names("Customer", self.customer.name), set())
        self.assertEqual(frappe.db.count("Chat Conversation"), before)

    def test_reference_permission_required(self):
        frappe.set_user("Guest")
        with self.assertRaises(frappe.PermissionError):
            find_chats("Lead", self.lead.name)

    def test_assigned_sales_user_reads_erp_chat(self):
        user = self.insert(
            "User",
            name=self.prefix + "@example.invalid",
            email=self.prefix + "@example.invalid",
            first_name="Chat Test",
            enabled=1,
            user_type="System User",
        )
        self.insert(
            "Has Role",
            parent=user.name,
            parenttype="User",
            parentfield="roles",
            role="Sales User",
        )
        chat = self.conversation()
        frappe.db.set_value("Lead", self.lead.name, "lead_owner", user.name)
        frappe.set_user(user.name)
        # Force the restricted branch so this exercises SQL and document permissions.
        with patch(
            "wa_chat_hub.permissions.has_unrestricted_chat_access", return_value=False
        ):
            self.assertTrue(can_read_conversation(str(chat.name)))
            self.assertEqual(self.names("Lead", self.lead.name), {str(chat.name)})
            frappe.set_user("Guest")
            self.assertFalse(can_read_conversation(str(chat.name)))

    def test_shared_lead_does_not_grant_other_chats(self):
        user = self.insert(
            "User",
            name=self.prefix + "@example.invalid",
            email=self.prefix + "@example.invalid",
            first_name="Shared Chat",
            enabled=1,
            user_type="System User",
        )
        self.insert(
            "DocShare",
            share_doctype="Lead",
            share_name=self.lead.name,
            user=user.name,
            read=1,
        )
        allowed = self.conversation()
        other = self.insert(
            "Lead",
            name=self.prefix + "-other",
            first_name="Other",
            lead_name="Other",
            status="Lead",
        )
        denied = self.conversation(name=other.name)
        frappe.set_user(user.name)
        with patch(
            "wa_chat_hub.permissions.has_unrestricted_chat_access", return_value=False
        ):
            rows = frappe.get_list(
                "Chat Conversation",
                filters={"name": ["in", [allowed.name, denied.name]]},
                pluck="name",
            )
            self.assertEqual({str(name) for name in rows}, {str(allowed.name)})
            self.assertFalse(can_read_conversation(str(denied.name)))
