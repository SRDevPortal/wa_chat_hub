import unittest
from wa_chat_hub.desk_privacy import project_list

class DeskPrivacyTests(unittest.TestCase):
    def test_compressed_list_masks_contact_and_preview_without_changing_identity(self):
        raw = {'keys': ['name', 'contact', 'last_message_preview', 'unread_count'],
               'values': [['193724', '919876501234', 'Call 9876501234', 2]]}
        result = project_list(raw)
        self.assertNotIn('9876501234', str(result))
        self.assertEqual(result['values'][0][0], '193724')
        self.assertEqual(result['values'][0][3], 2)
        self.assertEqual(raw['values'][0][1], '919876501234')

    def test_uncompressed_rows_hide_raw_workflow_payloads(self):
        result = project_list([{'name': '42', 'contact': '919876501234', 'ai_workflow_state': 'secret'}])
        self.assertEqual(result[0]['ai_workflow_state'], '')
        self.assertEqual(result[0]['contact'], '********1234')

    def test_form_save_preserves_contact_and_masked_text(self):
        from wa_chat_hub.desk_privacy import prepare_document, project_document
        raw = {'doctype': 'Chat Conversation', 'name': '42', 'contact': '919876501234',
               'last_message_preview': 'Call 9876501234', 'ai_workflow_state': 'raw', 'status': 'Open'}
        outgoing = project_document(raw)
        outgoing['status'] = 'Closed'
        result = prepare_document(outgoing, raw)
        self.assertEqual(result['contact'], raw['contact'])
        self.assertEqual(result['last_message_preview'], raw['last_message_preview'])
        self.assertEqual(result['ai_workflow_state'], 'raw')
        self.assertEqual(result['status'], 'Closed')
        self.assertNotIn('__wa_number_privacy', result)

    def test_masked_form_cannot_relink_contact(self):
        import frappe
        from wa_chat_hub.desk_privacy import prepare_document
        with self.assertRaises(frappe.PermissionError):
            prepare_document({'contact': 'another'}, {'contact': '919876501234'})

    def test_save_response_accepts_frappe_dict_and_document(self):
        import frappe
        from unittest.mock import patch
        from wa_chat_hub.desk_privacy import _project_docs
        for document in (frappe._dict(doctype="Chat Conversation", name="42", contact="919876501234"),
                         frappe.get_doc({"doctype": "Chat Conversation", "name": "42", "contact": "919876501234"})):
            with patch.object(frappe.local, "response", frappe._dict(docs=[document])):
                _project_docs()
                self.assertNotIn("919876501234", frappe.as_json(frappe.response))
                self.assertTrue(frappe.response.docs[0]["__wa_number_privacy"])

    def test_failed_projection_removes_raw_response(self):
        import frappe
        from unittest.mock import patch
        from wa_chat_hub import desk_privacy
        raw = frappe._dict(doctype="Chat Conversation", name="42", contact="919876501234")
        with patch.object(frappe.local, "response", frappe._dict(docs=[raw], docinfo={"contact": raw.contact})), \
             patch.object(desk_privacy, "project_document", side_effect=ValueError("projection failed")):
            with self.assertRaises(ValueError):
                desk_privacy._project_docs()
            self.assertNotIn("docs", frappe.response)
            self.assertNotIn("docinfo", frappe.response)

    def test_priority_temperature_save_restores_number_and_masks_response(self):
        import frappe
        from unittest.mock import patch, Mock
        from wa_chat_hub import desk_privacy
        from privacy_shield import desk
        raw = frappe._dict(doctype="Chat Conversation", name="42", contact="919876501234",
                          priority="Medium", lead_temperature="Cold")
        stored = Mock()
        stored.as_dict.return_value = raw
        outgoing = desk_privacy.project_document(raw)
        outgoing.update(priority="High", lead_temperature="Warm")
        def save(doc, action):
            data = frappe.parse_json(doc)
            self.assertEqual(data.contact, raw.contact)
            self.assertEqual(data.priority, "High")
            self.assertEqual(data.lead_temperature, "Warm")
            frappe.response.docs = [data]
        with patch.object(desk_privacy, "restricted", return_value=True), \
             patch.object(frappe, "get_doc", return_value=stored), \
             patch.object(desk, "savedocs", side_effect=save), \
             patch.object(frappe.local, "response", frappe._dict(docs=[])):
            desk_privacy.savedocs(frappe.as_json(outgoing), "Save")
            self.assertNotIn(raw.contact, frappe.as_json(frappe.response))
            self.assertEqual(frappe.response.docs[0]["priority"], "High")
            self.assertEqual(frappe.response.docs[0]["lead_temperature"], "Warm")
            self.assertEqual(stored.check_permission.call_count, 2)
