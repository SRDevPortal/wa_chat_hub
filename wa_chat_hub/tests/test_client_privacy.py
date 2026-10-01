import unittest
from unittest.mock import patch
from wa_chat_hub import client_privacy as api

class ClientPrivacyTests(unittest.TestCase):
    def test_contact_identity_and_phone_are_masked_without_mutation(self):
        raw = {'name':'919876501234', 'phone_number':'919876501234'}
        result=api.project_read(raw,'Chat Contact')
        self.assertNotIn('919876501234',str(result))
        self.assertEqual(raw['phone_number'],'919876501234')

    def test_tuple_results_and_aliases_are_masked(self):
        for raw in ([['919876501234']], {'aliased_phone':'919876501234'}):
            self.assertNotIn('919876501234',str(api.project_read(raw,'Chat Contact')))

    def test_conversation_ids_preserved_and_raw_payload_removed(self):
        result=api.project_read({'name':'195854','contact':'919876501234','ai_workflow_state':'raw'},'Chat Conversation')
        self.assertEqual(result['name'],'195854')
        self.assertEqual(result['ai_workflow_state'],'')
        self.assertNotIn('919876501234',str(result))

    def test_permissions_are_checked_by_existing_adapter(self):
        from privacy_shield import desk
        with patch.object(desk,'get',side_effect=PermissionError), patch.object(api,'restricted',return_value=True):
            with self.assertRaises(PermissionError):api.get.__wrapped__('Chat Contact','x')

    def test_full_visibility_and_other_doctypes_are_unchanged(self):
        from privacy_shield import desk
        raw={'name':'919876501234'}
        with patch.object(desk,'get',return_value=raw), patch.object(api,'restricted',return_value=False):
            self.assertIs(api.get.__wrapped__('Chat Contact','x'),raw)
        with patch.object(desk,'get',return_value=raw), patch.object(api,'restricted',return_value=True):
            self.assertIs(api.get.__wrapped__('Patient','x'),raw)

    def test_structured_references_preserved_in_both_read_paths(self):
        from wa_chat_hub.desk_privacy import project_document
        raw = {"linked_reference_name": "CRM-LEAD-2026-687089",
               "linked_crm_lead": "CRM-LEAD-2026-687089",
               "linked_patient": "HLC-PAT-2026-38721",
               "contact": "919876501234"}
        for result in (api.project_read(raw, "Chat Conversation"), project_document(raw)):
            for key in ("linked_reference_name", "linked_crm_lead", "linked_patient"):
                self.assertEqual(result[key], raw[key])
            self.assertNotIn(raw["contact"], str(result))

    def test_reference_fields_cannot_exempt_phone_values(self):
        from wa_chat_hub.desk_privacy import project_document
        for value in ("919876501234", "CRM-LEAD-2026-687089 919876501234"):
            raw = {"linked_reference_name": value, "source_name": value}
            for result in (api.project_read(raw, "Chat Conversation"), project_document(raw)):
                self.assertNotIn("919876501234", str(result))
