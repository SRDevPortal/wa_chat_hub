import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.agent_router import AgentRoute, build_agent_prompt
from wa_chat_hub.patient_names import (
    build_patient_name_context, get_patient_display_name, invalidate_patient_display_name,
)
from wa_chat_hub.services import _link_patient_to_conversation, _update_inbound_contact_display_name
from wa_chat_hub.tests.policy_fixtures import TEST_POLICY


class TestPatientNames(TestCase):
    def test_public_patient_messages_receive_registered_name(self):
        route = AgentRoute(patient="PAT-1", party_type="Patient", identity_status="Matched", requires_patient_data=False)
        with patch("wa_chat_hub.patient_names.get_patient_display_name", return_value="Amit") as read:
            context = build_patient_name_context(route)
        self.assertEqual(json.loads(context), {"patient_name": "Amit"})
        read.assert_called_once_with("PAT-1")

    def test_unmatched_or_ambiguous_chats_do_not_load_patient_names(self):
        with patch("wa_chat_hub.patient_names.get_patient_display_name") as read:
            for patient, status in ((None, "Matched"), ("PAT-1", "Ambiguous"), ("PAT-1", "Unverified")):
                self.assertEqual(build_patient_name_context(AgentRoute(patient=patient, identity_status=status)), "")
        read.assert_not_called()

    def test_empty_name_is_not_invented(self):
        with patch("wa_chat_hub.patient_names.get_patient_display_name", return_value=""):
            self.assertEqual(build_patient_name_context(AgentRoute(patient="PAT-1", identity_status="Matched")), "")

    def test_saved_prompts_are_used_verbatim_without_runtime_rewriting(self):
        route = AgentRoute(
            patient="PAT-1", party_type="Patient", identity_status="Matched",
            system_prompt="Use the greeting configured by the clinic.",
            prompt_overlay="Use the department's preferred style.",
        )
        self.assertEqual(build_agent_prompt(route), route.system_prompt + "\n\n" + route.prompt_overlay)
        route.system_prompt = "Never ask for or confirm the patient's name."
        self.assertEqual(build_agent_prompt(route), route.system_prompt + "\n\n" + route.prompt_overlay)

    def test_patient_context_instructions_come_from_saved_policy(self):
        route = AgentRoute(
            patient="PAT-1", party_type="Patient", identity_status="Matched", policy_bundle=TEST_POLICY,
            system_prompt="Configured care instructions.",
        )
        with patch("wa_chat_hub.agent_router._route_policy_reply", return_value="Clinic-configured context.") as read:
            self.assertEqual(build_agent_prompt(route), "Configured care instructions.\n\nClinic-configured context.")
        read.assert_called_once_with(route, "system_identity_verified")

    def test_repeated_name_reads_use_one_scalar_query(self):
        with (
            patch("wa_chat_hub.patient_names.frappe") as api,
            patch("wa_chat_hub.patient_names.assert_ai_doctype_permission"),
            patch("wa_chat_hub.patient_names.safe_ai_get_value", return_value="Amit") as read,
        ):
            values = {}
            api.cache.return_value.get_value.side_effect = lambda key, **kwargs: values.get(key)
            api.cache.return_value.set_value.side_effect = lambda key, value, **kwargs: values.update({key: value})
            api.db.after_commit.add.side_effect = lambda callback: callback()
            for _ in range(10):
                self.assertEqual(get_patient_display_name("PAT-1"), "Amit")
            read.assert_called_once_with("Patient", "PAT-1", "patient_name")

    def test_patient_rename_invalidates_name_cache_at_commit(self):
        doc = frappe._dict(name="PAT-1", patient_name="New", get_doc_before_save=lambda: frappe._dict(patient_name="Old"))
        with patch("wa_chat_hub.patient_names.frappe") as api:
            invalidate_patient_display_name(doc, "on_update")
            self.assertEqual(api.cache.return_value.set_value.call_args.args[0], "wa_patient_name:v1:PAT-1:revision")
            api.db.after_commit.add.assert_called_once()
            api.db.after_commit.add.call_args.args[0]()
            self.assertEqual(api.cache.return_value.set_value.call_count, 2)

    def test_linked_patient_display_name_is_not_overwritten_by_whatsapp_nickname(self):
        with (
            patch("wa_chat_hub.services.safe_ai_get_value", return_value={"linked_patient": "PAT-1", "display_name": "Amit"}),
            patch("wa_chat_hub.services.safe_ai_set_value") as write,
        ):
            _update_inbound_contact_display_name("CONTACT-1", "Nickname")
        write.assert_not_called()

    def test_patient_link_corrects_display_once_and_keeps_repeats_write_free(self):
        convo = SimpleNamespace(name="CHAT-1", linked_patient="PAT-1", identity_status="Matched",
                                linked_reference_doctype="Patient", linked_reference_name="PAT-1", routing_version=3)
        contact = SimpleNamespace(name="CONTACT-1", linked_patient="PAT-1", source_doctype="Patient",
                                  source_name="PAT-1", display_name="Nickname")
        with (
            patch("wa_chat_hub.patient_names.get_patient_display_name", return_value="Amit"),
            patch("wa_chat_hub.services._apply_vobiz_patient_routing"),
            patch("wa_chat_hub.services.with_db_lock_retry", side_effect=lambda label, callback: callback()),
            patch("wa_chat_hub.services.safe_ai_set_value") as write,
        ):
            for _ in range(2):
                _link_patient_to_conversation(conversation="CHAT-1", convo=convo, contact=contact,
                                              patient_name="PAT-1", phone_number="919000000001", display_name="Nickname")
        self.assertEqual(contact.display_name, "Amit")
        write.assert_called_once_with("Chat Contact", "CONTACT-1", {"display_name": "Amit"}, update_modified=False)
