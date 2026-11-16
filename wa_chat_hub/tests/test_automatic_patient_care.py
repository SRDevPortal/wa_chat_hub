from contextlib import ExitStack
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from wa_chat_hub.agent_router import AgentRoute, apply_intent_route, persist_agent_route
from wa_chat_hub.ai.configuration import IntentDefinition, IntentRouteDefinition, resolve_intent_route
from wa_chat_hub.identity import reconcile_conversation_identity, refresh_changed_contact_phone
from wa_chat_hub.mcp.patient_records import _verified_patient
from wa_chat_hub.patient_matching import find_patient_matches, invalidate_patient_matches
from wa_chat_hub.patches.v1_1.automatic_patient_care import policy_updates, _save_changes
from wa_chat_hub.services import _link_or_create_master_record, _link_patient_to_conversation
from wa_chat_hub.tests.policy_fixtures import TEST_POLICY


class TestAutomaticPatientCare(TestCase):
    def test_matched_patient_stays_with_care_agent_for_greetings(self):
        route = AgentRoute(
            patient="PAT-1", party_type="Patient", identity_status="Matched",
            agent_profile="Patient Care Agent", agent_type="Patient",
            allowed_tool_names={"patient_profile"}, policy_bundle=TEST_POLICY,
        )
        definition = IntentDefinition(name="greeting", label="Greeting", description="Greeting")
        with (
            patch("wa_chat_hub.ai.configuration.load_active_intents", return_value={"greeting": definition}),
            patch("wa_chat_hub.ai.configuration.resolve_intent_route", return_value=IntentRouteDefinition(name="Public")),
            patch("wa_chat_hub.agent_router.frappe.get_doc") as get_doc,
        ):
            result = apply_intent_route(route, SimpleNamespace(intent="greeting"))
        self.assertEqual(result.agent_profile, "Patient Care Agent")
        self.assertEqual(result.allowed_tool_names, {"patient_profile"})
        self.assertFalse(result.requires_patient_data)
        get_doc.assert_not_called()

    def test_existing_verified_shipping_route_also_matches_automatic_patient(self):
        definition = IntentDefinition(name="shipping", label="Shipping", description="Shipping", requires_patient_data=True)
        rows = [frappe._dict(
            name="Shipment", intent="shipping", party_type="Patient", identity_status="Verified",
            target_type="Agent Profile", agent_profile="Patient Care Agent",
            preferred_mcp_tool="shipping_history", priority=100,
        )]
        with (
            patch("wa_chat_hub.ai.configuration._doctype_available", return_value=True),
            patch("wa_chat_hub.ai.configuration.safe_ai_get_all", return_value=rows) as query,
        ):
            route = resolve_intent_route(
                definition, channel_account="Account", party_type="Patient",
                identity_status="Matched", equivalent_identity_status="Verified",
            )
        self.assertEqual(route.preferred_mcp_tool, "shipping_history")
        query.assert_called_once()

    def test_record_access_is_limited_to_matched_patient(self):
        for status in ("Matched", "Verified"):
            with self.subTest(status=status), patch(
                "wa_chat_hub.mcp.patient_records.safe_ai_get_doc",
                return_value=SimpleNamespace(linked_patient="PAT-1", identity_status=status),
            ) as read:
                self.assertEqual(_verified_patient(patient="PAT-1", conversation="CHAT-1"), "PAT-1")
                read.assert_called_once_with("Chat Conversation", "CHAT-1")

    def test_missing_ambiguous_and_cross_patient_access_is_rejected(self):
        for patient, status in (("PAT-2", "Matched"), ("PAT-1", "Ambiguous"), (None, "Matched"), ("PAT-1", "Unverified")):
            with (
                self.subTest(patient=patient, status=status),
                patch("wa_chat_hub.mcp.patient_records.safe_ai_get_doc", return_value=SimpleNamespace(linked_patient=patient, identity_status=status)),
                patch("wa_chat_hub.mcp.patient_records._", side_effect=lambda value: value),
                patch("wa_chat_hub.mcp.patient_records.frappe.throw", side_effect=frappe.PermissionError),
            ):
                with self.assertRaises(frappe.PermissionError):
                    _verified_patient(patient="PAT-1", conversation="CHAT-1")

    def test_unchanged_routing_does_not_write(self):
        route = AgentRoute(
            agent_profile="Patient Care Agent", party_type="Patient",
            routing_reason="patient_identity:automatic_patient_care",
        )
        route.stored_values = {
            "party_type": "Patient", "agent_profile": "Patient Care Agent",
            "department_source": None, "department_confidence": 0.0,
            "routing_reason": route.routing_reason, "routing_version": 3,
        }
        with patch("wa_chat_hub.agent_router.frappe") as api:
            api.get_meta.return_value.has_field.return_value = True
            persist_agent_route("CHAT-1", route)
            api.db.set_value.assert_not_called()
            route.agent_profile = "Another Care Agent"
            persist_agent_route("CHAT-1", route)
            api.db.set_value.assert_called_once_with(
                "Chat Conversation", "CHAT-1", {"agent_profile": "Another Care Agent"}, update_modified=False
            )

    def test_unchanged_identity_does_not_refresh_timestamp_or_reload(self):
        convo = SimpleNamespace(
            name="CHAT-1", linked_patient="PAT-1", party_type="Patient", identity_status="Matched",
            linked_reference_doctype="Patient", linked_reference_name="PAT-1",
            linked_crm_lead=None, last_identity_sync_at="earlier",
        )
        with (
            patch("wa_chat_hub.identity.frappe") as api,
            patch("wa_chat_hub.identity.get_conversation_policy", return_value=TEST_POLICY),
        ):
            api.db.exists.return_value = True
            api.get_meta.side_effect = lambda dt: SimpleNamespace(has_field=lambda field: dt == "Chat Conversation")
            result = reconcile_conversation_identity(convo, patient="PAT-1", source="inbound_phone_match")
            self.assertFalse(result["changed"])
            self.assertEqual(convo.last_identity_sync_at, "earlier")
            api.db.set_value.assert_not_called()
            api.get_doc.assert_not_called()

    def test_repeated_link_does_not_write_reconcile_reload_or_enqueue(self):
        convo = SimpleNamespace(
            name="CHAT-1", linked_patient="PAT-1", identity_status="Matched",
            linked_reference_doctype="Patient", linked_reference_name="PAT-1", channel_account="Account",
            routing_version=3,
        )
        contact = SimpleNamespace(name="CONTACT-1", linked_patient="PAT-1", source_doctype="Patient", source_name="PAT-1", display_name="Amit")
        with (
            patch("wa_chat_hub.patient_names.get_patient_display_name", return_value="Amit"),
            patch("wa_chat_hub.services._apply_vobiz_patient_routing"),
            patch("wa_chat_hub.services.safe_ai_set_value") as write,
            patch("wa_chat_hub.identity.reconcile_conversation_identity") as reconcile,
            patch("wa_chat_hub.interakt.contact_sync.enqueue_push_for_conversation") as enqueue,
        ):
            _link_patient_to_conversation(conversation=convo.name, convo=convo, contact=contact, patient_name="PAT-1", phone_number="919000000001")
        write.assert_not_called()
        reconcile.assert_not_called()
        enqueue.assert_not_called()

    def test_switching_patient_does_not_inherit_old_verification(self):
        convo = SimpleNamespace(
            name="CHAT-1", linked_patient="OLD", identity_status="Verified",
            linked_reference_doctype="Patient", linked_reference_name="OLD", linked_crm_lead=None,
        )
        with (
            patch("wa_chat_hub.identity.frappe") as api,
            patch("wa_chat_hub.identity.now_datetime", return_value="now"),
            patch("wa_chat_hub.identity.get_conversation_policy", return_value=TEST_POLICY),
        ):
            api.db.exists.return_value = True
            api.get_meta.side_effect = lambda dt: SimpleNamespace(has_field=lambda field: dt == "Chat Conversation")
            result = reconcile_conversation_identity(convo, patient="NEW", source="inbound_phone_match")
        self.assertEqual(result["values"]["identity_status"], "Matched")
        self.assertIsNone(result["values"]["verification_completed_at"])

    def test_patient_match_overrides_preserved_lead_link(self):
        convo = SimpleNamespace(name="CHAT-1", linked_crm_lead="LEAD-1", channel_account="Account")
        policy = replace(TEST_POLICY, sections={**TEST_POLICY.sections, "party_routing_policy": {
            **TEST_POLICY.section("party_routing_policy"), "phone_match_priority": ["Patient", "CRM Lead"],
            "preserve_existing_reference": True,
        }})
        with ExitStack() as stack:
            stack.enter_context(patch("wa_chat_hub.services.safe_ai_get_doc", side_effect=[convo, SimpleNamespace(name="CONTACT-1")]))
            for name in ("_sanitize_contact_links", "_sanitize_conversation_links", "_normalize_existing_lead_link"):
                stack.enter_context(patch("wa_chat_hub.services." + name))
            stack.enter_context(patch("wa_chat_hub.services.get_channel_policy", return_value=policy))
            stack.enter_context(patch("wa_chat_hub.patient_matching.find_patient_matches", return_value={"PAT-1"}))
            link = stack.enter_context(patch("wa_chat_hub.services._link_patient_to_conversation"))
            create_lead = stack.enter_context(patch("wa_chat_hub.services._create_lead_for_inbound"))
            _link_or_create_master_record("CHAT-1", "CONTACT-1", "919000000001")
        self.assertEqual(link.call_args.kwargs["patient_name"], "PAT-1")
        create_lead.assert_not_called()

    def test_contact_phone_change_clears_previous_patient_access(self):
        contact = MagicMock(name="Contact")
        contact.name = "CONTACT-1"
        contact.get.return_value = "919000000002"
        contact.get_doc_before_save.return_value.get.return_value = "919000000001"
        with patch("wa_chat_hub.identity.frappe") as api, patch("wa_chat_hub.services._clear_stale_patient_link") as clear:
            api.get_all.return_value = ["CHAT-1"]
            refresh_changed_contact_phone(contact)
            clear.assert_called_once_with(api.get_doc.return_value, contact)


class TestPatientMatchCache(TestCase):
    def test_repeat_messages_and_unknown_numbers_need_one_lookup(self):
        for matches in ({"PAT-1"}, set(), {"PAT-1", "PAT-2"}):
            with (
                self.subTest(matches=matches),
                patch("wa_chat_hub.patient_matching.frappe") as api,
                patch("wa_chat_hub.services.assert_ai_doctype_permission"),
                patch("wa_chat_hub.services._find_indexed_phone_match_names", return_value=matches) as lookup,
            ):
                values = {}
                api.cache.return_value.get_value.side_effect = lambda key, **kwargs: values.get(key)
                api.cache.return_value.set_value.side_effect = lambda key, value, **kwargs: values.update({key: value})
                api.db.after_commit.add.side_effect = lambda callback: callback()
                for _ in range(10):
                    self.assertEqual(find_patient_matches("919000000001", ["mobile"]), matches)
                lookup.assert_called_once_with("Patient", ["mobile"], "919000000001", limit=2)

    def test_patient_phone_edits_invalidate_old_and_new_numbers_after_commit(self):
        old = frappe._dict(mobile="9000000001")
        doc = frappe._dict(mobile="9000000002", get_doc_before_save=lambda: old)
        with patch("wa_chat_hub.patient_matching.frappe") as api:
            invalidate_patient_matches(doc, "on_update")
            invalidated = {call.args[0] for call in api.cache.return_value.set_value.call_args_list}
            self.assertEqual(invalidated, {"wa_patient_match:v1:9000000001:revision", "wa_patient_match:v1:9000000002:revision"})
            api.db.after_commit.add.assert_called_once()
            api.db.after_commit.add.call_args.args[0]()
            self.assertEqual(api.cache.return_value.set_value.call_count, 4)

    def test_unrelated_patient_edits_do_not_invalidate_cache(self):
        doc = frappe._dict(mobile="9000000001", get_doc_before_save=lambda: frappe._dict(mobile="9000000001"))
        with patch("wa_chat_hub.patient_matching.frappe") as api:
            invalidate_patient_matches(doc, "on_update")
            api.cache.assert_not_called()

    def test_results_are_published_only_after_transaction_commit(self):
        with (
            patch("wa_chat_hub.patient_matching.frappe") as api,
            patch("wa_chat_hub.services.assert_ai_doctype_permission"),
            patch("wa_chat_hub.services._find_indexed_phone_match_names", return_value={"PAT-1"}),
        ):
            api.cache.return_value.get_value.return_value = None
            self.assertEqual(find_patient_matches("9000000001", ["mobile"]), {"PAT-1"})
            api.cache.return_value.set_value.assert_not_called()
            api.db.after_commit.add.call_args.args[0]()
            api.cache.return_value.set_value.assert_called_once()

    def test_late_old_lookup_cannot_repopulate_current_revision(self):
        with (
            patch("wa_chat_hub.patient_matching.frappe") as api,
            patch("wa_chat_hub.services.assert_ai_doctype_permission"),
            patch("wa_chat_hub.services._find_indexed_phone_match_names", side_effect=[{"OLD"}, {"NEW"}]) as lookup,
        ):
            values = {}
            callbacks = []
            api.cache.return_value.get_value.side_effect = lambda key, **kwargs: values.get(key)
            api.cache.return_value.set_value.side_effect = lambda key, value, **kwargs: values.update({key: value})
            api.db.after_commit.add.side_effect = callbacks.append
            find_patient_matches("9000000001", ["mobile"])
            values["wa_patient_match:v1:9000000001:revision"] = "new-revision"
            callbacks.pop()()  # An old reader completes after the edit commits.
            self.assertEqual(find_patient_matches("9000000001", ["mobile"]), {"NEW"})
            self.assertEqual(lookup.call_count, 2)


class TestAutomaticPatientMigration(TestCase):
    def test_migration_retires_existing_verification_and_is_safe_to_rerun(self):
        from wa_chat_hub.patches.v1_1.automatic_patient_care import execute

        class Doc(frappe._dict):
            def set(self, key, value):
                self[key] = value

        care = Doc(name="Custom Care", agent_type="Patient", system_prompt="Keep custom instructions", allowed_tools=[], save=MagicMock())
        verification = Doc(name="Old Verification", agent_type="Patient Verification", is_active=1, allowed_tools=[], save=MagicMock())
        route = Doc(name="Custom Matched Route", agent_profile="Old Verification", identity_status="Matched", blocked_reply="Verify first", save=MagicMock())
        intent = Doc(is_active=1, save=MagicMock())
        endpoint = Doc(is_active=1, save=MagicMock())
        records = {"WA AI Agent Profile": [care, verification], "WA AI Intent Route": [route]}
        with (
            patch("wa_chat_hub.patches.v1_1.automatic_patient_care._records", side_effect=lambda doctype: records.get(doctype, [])),
            patch("wa_chat_hub.patches.v1_1.automatic_patient_care.frappe") as api,
        ):
            api.db.get_value.return_value = care.name
            api.db.exists.return_value = True
            api.get_doc.side_effect = lambda doctype, name: intent if doctype == "WA AI Intent" else endpoint
            execute()
            execute()
        self.assertEqual(route.agent_profile, "Custom Care")
        self.assertEqual(route.blocked_reply, "")
        self.assertEqual(verification.is_active, 0)
        self.assertEqual(care.require_verified_identity, 0)
        self.assertEqual("Keep custom instructions", care.system_prompt)
        for doc in (care, verification, route, intent, endpoint):
            doc.save.assert_called_once()

    def test_policy_migration_is_idempotent_and_preserves_custom_settings(self):
        doc = frappe._dict(
            identity_policy=json.dumps(TEST_POLICY.section("identity_policy")),
            party_routing_policy=json.dumps({**TEST_POLICY.section("party_routing_policy"), "custom_key": "keep"}),
            reply_templates=json.dumps({"custom_reply": "keep"}),
        )
        first = policy_updates(doc)
        doc.update(first)
        self.assertEqual(policy_updates(doc), first)
        self.assertEqual(json.loads(first["identity_policy"])["validity_hours"], 0)
        self.assertEqual(json.loads(first["party_routing_policy"])["custom_key"], "keep")
        self.assertEqual(json.loads(first["reply_templates"]), {"custom_reply": "keep"})

    def test_unchanged_migration_record_is_not_saved(self):
        doc = MagicMock()
        doc.get.return_value = 0
        _save_changes(doc, {"is_active": 0})
        doc.save.assert_not_called()
