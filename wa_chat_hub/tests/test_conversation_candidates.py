from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch
import frappe
from wa_chat_hub import permissions, identity
from wa_chat_hub.api import chat


class ConversationCandidateTests(TestCase):
    def test_numeric_id_loads_document_before_policy_check(self):
        doc = SimpleNamespace(name=123)
        with patch.object(frappe, "db", MagicMock()) as db, \
             patch.object(frappe, "get_doc", return_value=doc) as load, \
             patch.object(identity, "get_conversation_policy", return_value=None) as policy:
            self.assertEqual(identity.reconcile_conversation_identity(123)["reason"], "identity_policy_missing")
        db.exists.assert_called_once_with("Chat Conversation", "123")
        load.assert_called_once_with("Chat Conversation", "123")
        policy.assert_called_once_with(doc)

    def test_document_input_keeps_existing_policy_path(self):
        doc = SimpleNamespace(name="123")
        with patch.object(frappe, "get_doc") as load, \
             patch.object(identity, "get_conversation_policy", return_value=None) as policy:
            identity.reconcile_conversation_identity(doc)
        load.assert_not_called()
        policy.assert_called_once_with(doc)

    def test_candidate_branches_keep_full_permission_predicate(self):
        condition = "wa_lead.lead_owner = 'agent' AND wa_lead.sr_lead_pipeline = 'P'"
        with patch.object(permissions, "_user", return_value="agent"), \
             patch.object(permissions, "has_unrestricted_chat_access", return_value=False), \
             patch.object(permissions, "crm_lead_permission_condition", return_value=condition), \
             patch.object(frappe, "db", MagicMock()):
            sql = permissions.conversation_access_sql_join()
        self.assertEqual(sql.count(condition), 2)
        self.assertIn(" UNION ", sql)
        self.assertIn("IN ('CRM Lead', 'Lead')", sql)
        self.assertIn("wa_access.name = c.name", sql)

    def test_no_permission_and_unrestricted_leave_original_predicate_in_charge(self):
        for unrestricted, condition in ((True, ""), (False, "1=0")):
            with patch.object(permissions, "_user", return_value="agent"), \
                 patch.object(permissions, "has_unrestricted_chat_access", return_value=unrestricted), \
                 patch.object(permissions, "crm_lead_permission_condition", return_value=condition):
                self.assertEqual(permissions.conversation_access_sql_join(), "")

    def test_list_retains_original_permission_filter_and_bound_values(self):
        db = MagicMock()
        with patch.object(frappe, "db", db), \
             patch.object(chat, "conversation_access_sql_join", return_value="JOIN candidates wa_access ON wa_access.name=c.name"), \
             patch.object(chat, "conversation_access_sql_condition", return_value="original_permission"), \
             patch.object(chat, "_has_conversation_last_message_time", return_value=True):
            chat._conversation_sql_rows({"status": "Open"}, 20)
        query, values = db.sql.call_args.args
        self.assertIn("original_permission", query)
        self.assertIn("JOIN candidates", query)
        self.assertNotIn("'Open'", query)
        self.assertEqual(values["filter_0"], "Open")
        self.assertEqual(values["limit"], 100)
