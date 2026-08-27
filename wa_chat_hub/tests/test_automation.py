from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wa_chat_hub.automation import resolve_patient_route, send_patient_template
from wa_chat_hub.channel_resolver import get_or_create_patient_conversation_for_channel_account
from wa_chat_hub.interakt.templates_api import resolve_approved_template
from wa_chat_hub.messaging.channel_map import resolve_patient_department_map
from wa_chat_hub.wa_chat_hub.doctype.wa_channel_pipeline_map.wa_channel_pipeline_map import (
    WAChannelPipelineMap,
)


class TestAutomatedPatientTemplate(FrappeTestCase):
    def test_routes_sends_and_persists_template(self):
        patient = frappe._dict(name="PAT-0001")
        account = frappe._dict(name="Interakt Test", is_active=1, channel_type="Interakt")
        contact = frappe._dict(name="CONTACT-0001", phone_number="919876543210")
        call_order = []

        def get_doc(doctype, name):
            return {
                "Patient": patient,
                "Chat Channel Account": account,
                "Chat Contact": contact,
            }[doctype]

        with (
            patch("wa_chat_hub.automation.set_service_user_context"),
            patch("wa_chat_hub.automation.safe_ai_get_doc", side_effect=get_doc),
            patch(
                "wa_chat_hub.automation.resolve_patient_route",
                return_value={
                    "conversation": "CONV-0001",
                    "channel_account": "Interakt Test",
                    "contact": "CONTACT-0001",
                    "routing_source": "Department Map",
                },
            ),
            patch(
                "wa_chat_hub.automation.send_interakt_template_message",
                side_effect=lambda *args: (
                    call_order.append("provider")
                    or {
                        "sent": True,
                        "delivery_status": "Sent",
                        "provider_message_id": "MSG-0001",
                    }
                ),
            ) as send_template,
            patch(
                "wa_chat_hub.automation.resolve_approved_template",
                side_effect=lambda channel_account, template: template,
            ),
            patch(
                "wa_chat_hub.automation.append_message",
                return_value={"message": "CHAT-MSG-0001"},
            ) as append_message,
            patch(
                "wa_chat_hub.automation.frappe.db.commit",
                side_effect=lambda: call_order.append("commit"),
            ) as commit,
            patch("wa_chat_hub.automation.frappe.set_user"),
        ):
            result = send_patient_template(
                patient="PAT-0001",
                template_name="order_picked_up",
                language_code="en",
                body_values=["Patient", "ORDER-0001"],
                body_preview="Hello Patient, your order ORDER-0001 was picked up.",
                event_key="shipment:SHIP-0001:order_picked_up",
                fallback_channel_account="Fallback Interakt",
            )

        self.assertEqual(result["message"], "CHAT-MSG-0001")
        self.assertEqual(result["provider_message_id"], "MSG-0001")
        self.assertEqual(result["routing_source"], "Department Map")
        self.assertEqual(result["channel_account"], "Interakt Test")
        commit.assert_called_once()
        self.assertEqual(call_order, ["commit", "provider"])
        self.assertEqual(
            send_template.call_args.args[1]["template_name"],
            "order_picked_up",
        )
        self.assertEqual(
            append_message.call_args.args[0]["sender_type"],
            "System",
        )
        self.assertEqual(
            append_message.call_args.args[0]["dedupe_key"],
            "shipment:SHIP-0001:order_picked_up",
        )
        self.assertEqual(
            append_message.call_args.args[0]["body"],
            "Hello Patient, your order ORDER-0001 was picked up.",
        )
        self.assertEqual(
            append_message.call_args.args[0]["raw_transport_payload"]["body_values"],
            ["Patient", "ORDER-0001"],
        )

    def test_approved_template_rejects_body_variable_mismatch(self):
        template = {
            "template_name": "order_picked_up",
            "language_code": "en",
            "body_values": ["Patient", "ORDER-0001"],
        }
        approved = [
            {
                "name": "order_picked_up",
                "language_code": "en",
                "languages": ["en"],
                "body_variable_count": 5,
            }
        ]

        with patch(
            "wa_chat_hub.interakt.templates_api.fetch_approved_templates",
            return_value=approved,
        ):
            with self.assertRaises(frappe.ValidationError):
                resolve_approved_template("Testing Interakt", template)

    def test_existing_conversation_wins_over_mapping_and_fallback(self):
        patient = frappe._dict(name="PAT-0001")
        existing = {
            "conversation": "CONV-0001",
            "channel_account": "Existing Interakt",
            "contact": "CONTACT-0001",
            "routing_source": "Existing Conversation",
        }
        with (
            patch("wa_chat_hub.automation.find_existing_patient_route", return_value=existing),
            patch("wa_chat_hub.automation.resolve_patient_department_map") as get_map,
            patch(
                "wa_chat_hub.automation.get_or_create_patient_conversation_for_channel_account"
            ) as create_conversation,
        ):
            result = resolve_patient_route(patient, fallback_channel_account="Fallback Interakt")

        self.assertEqual(result, existing)
        get_map.assert_not_called()
        create_conversation.assert_not_called()

    def test_department_mapping_precedes_fallback(self):
        patient = frappe._dict(name="PAT-0001")
        with (
            patch("wa_chat_hub.automation.find_existing_patient_route", return_value=None),
            patch(
                "wa_chat_hub.automation.resolve_patient_department_map",
                return_value={
                    "name": "MAP-0001",
                    "chat_channel_account": "Mapped Interakt",
                    "sr_lead_pipeline": "PIPELINE-0001",
                    "is_default": 0,
                },
            ),
            patch(
                "wa_chat_hub.automation.get_or_create_patient_conversation_for_channel_account",
                return_value={
                    "conversation": "CONV-0001",
                    "channel_account": "Mapped Interakt",
                    "contact": "CONTACT-0001",
                },
            ) as create_conversation,
        ):
            result = resolve_patient_route(patient, fallback_channel_account="Fallback Interakt")

        self.assertEqual(result["routing_source"], "Department Map")
        self.assertEqual(create_conversation.call_args.args[1], "Mapped Interakt")

    def test_department_default_disambiguates_multiple_department_maps(self):
        patient = frappe._dict(name="PAT-0001", sr_medical_department="Kidney")
        with (
            patch("wa_chat_hub.automation.find_existing_patient_route", return_value=None),
            patch(
                "wa_chat_hub.automation.resolve_patient_department_map",
                return_value={
                    "name": "Testing Interakt",
                    "chat_channel_account": "Testing Interakt",
                    "sr_lead_pipeline": "Kidney YT Dom",
                    "is_default": 0,
                    "is_department_default": 1,
                },
            ),
            patch(
                "wa_chat_hub.automation.get_or_create_patient_conversation_for_channel_account",
                return_value={
                    "conversation": "CONV-0001",
                    "channel_account": "Testing Interakt",
                    "contact": "CONTACT-0001",
                },
            ) as create_conversation,
        ):
            result = resolve_patient_route(patient, fallback_channel_account="Testing Interakt")

        self.assertEqual(result["routing_source"], "Department Default")
        self.assertEqual(create_conversation.call_args.args[1], "Testing Interakt")

    def test_missing_map_uses_configured_fallback(self):
        patient = frappe._dict(name="PAT-0001")
        with (
            patch("wa_chat_hub.automation.find_existing_patient_route", return_value=None),
            patch(
                "wa_chat_hub.automation.resolve_patient_department_map",
                return_value=None,
            ),
            patch(
                "wa_chat_hub.automation.get_or_create_patient_conversation_for_channel_account",
                return_value={
                    "conversation": "CONV-0001",
                    "channel_account": "Fallback Interakt",
                    "contact": "CONTACT-0001",
                },
            ) as create_conversation,
        ):
            result = resolve_patient_route(patient, fallback_channel_account="Fallback Interakt")

        self.assertEqual(result["routing_source"], "Patient Notification Settings Fallback")
        self.assertEqual(create_conversation.call_args.args[1], "Fallback Interakt")

    def test_missing_department_uses_configured_fallback(self):
        patient = frappe._dict(name="PAT-0001")
        with (
            patch("wa_chat_hub.automation.find_existing_patient_route", return_value=None),
            patch(
                "wa_chat_hub.automation.resolve_patient_department_map",
                return_value=None,
            ),
            patch(
                "wa_chat_hub.automation.get_or_create_patient_conversation_for_channel_account",
                return_value={
                    "conversation": "CONV-0001",
                    "channel_account": "Fallback Interakt",
                    "contact": "CONTACT-0001",
                },
            ) as create_conversation,
        ):
            result = resolve_patient_route(patient, fallback_channel_account="Fallback Interakt")

        self.assertEqual(result["routing_source"], "Patient Notification Settings Fallback")
        self.assertEqual(create_conversation.call_args.args[1], "Fallback Interakt")

    def test_multiple_department_defaults_are_not_hidden_by_fallback(self):
        patient = frappe._dict(name="PAT-0001")
        with (
            patch("wa_chat_hub.automation.find_existing_patient_route", return_value=None),
            patch(
                "wa_chat_hub.automation.resolve_patient_department_map",
                side_effect=frappe.ValidationError(
                    "Multiple active default routes are configured for Medical Department Kidney."
                ),
            ),
            patch(
                "wa_chat_hub.automation.get_or_create_patient_conversation_for_channel_account"
            ) as create_conversation,
        ):
            with self.assertRaises(frappe.ValidationError):
                resolve_patient_route(patient, fallback_channel_account="Fallback Interakt")

        create_conversation.assert_not_called()

    def test_department_resolver_selects_only_department_default_from_multiple_maps(self):
        patient = frappe._dict(name="PAT-0001", sr_medical_department="Kidney")
        rows = [
            frappe._dict(
                name="MAP-0001",
                chat_channel_account="Interakt One",
                is_department_default=0,
            ),
            frappe._dict(
                name="MAP-0002",
                chat_channel_account="Interakt Two",
                is_department_default=1,
            ),
        ]
        with (
            patch("wa_chat_hub.messaging.channel_map._linked_record_exists", return_value=True),
            patch("wa_chat_hub.messaging.channel_map.safe_ai_get_all", return_value=rows) as get_all,
            patch("wa_chat_hub.messaging.channel_map._validate_channel_account") as validate_account,
        ):
            result = resolve_patient_department_map(patient)

        self.assertEqual(result.name, "MAP-0002")
        self.assertEqual(
            get_all.call_args.kwargs["filters"],
            {"sr_medical_department": "Kidney", "is_active": 1},
        )
        self.assertEqual(get_all.call_args.kwargs["limit_start"], 0)
        self.assertEqual(get_all.call_args.kwargs["limit_page_length"], 20)
        validate_account.assert_called_once_with("Interakt Two")

    def test_department_resolver_returns_none_when_multiple_maps_have_no_default(self):
        patient = frappe._dict(name="PAT-0001", sr_medical_department="Kidney")
        rows = [
            frappe._dict(name="MAP-0001", chat_channel_account="Interakt One"),
            frappe._dict(name="MAP-0002", chat_channel_account="Interakt Two"),
        ]
        with (
            patch("wa_chat_hub.messaging.channel_map._linked_record_exists", return_value=True),
            patch("wa_chat_hub.messaging.channel_map.safe_ai_get_all", return_value=rows),
            patch("wa_chat_hub.messaging.channel_map._validate_channel_account") as validate_account,
        ):
            result = resolve_patient_department_map(patient)

        self.assertIsNone(result)
        validate_account.assert_not_called()

    def test_department_resolver_rejects_multiple_department_defaults(self):
        patient = frappe._dict(name="PAT-0001", sr_medical_department="Kidney")
        rows = [
            frappe._dict(
                name="MAP-0001",
                chat_channel_account="Interakt One",
                is_department_default=1,
            ),
            frappe._dict(
                name="MAP-0002",
                chat_channel_account="Interakt Two",
                is_department_default=1,
            ),
        ]
        with (
            patch("wa_chat_hub.messaging.channel_map._linked_record_exists", return_value=True),
            patch("wa_chat_hub.messaging.channel_map.safe_ai_get_all", return_value=rows),
        ):
            with self.assertRaises(frappe.ValidationError):
                resolve_patient_department_map(patient)

    def test_department_default_validation_is_scoped_to_medical_department(self):
        pipeline_map = frappe._dict(name="MAP-0002", sr_medical_department="Kidney")
        with patch(
            "wa_chat_hub.wa_chat_hub.doctype.wa_channel_pipeline_map.wa_channel_pipeline_map.frappe.db.get_value",
            return_value="MAP-0001",
        ) as get_value:
            with self.assertRaises(frappe.ValidationError):
                WAChannelPipelineMap._validate_single_department_default(pipeline_map)

        self.assertEqual(
            get_value.call_args.args[1],
            {
                "name": ["!=", "MAP-0002"],
                "sr_medical_department": "Kidney",
                "is_active": 1,
                "is_department_default": 1,
            },
        )

    def test_explicit_patient_account_allows_missing_pipeline_map(self):
        patient = frappe._dict(name="PAT-0001", doctype="Patient")
        account = frappe._dict(is_active=1, channel_type="Interakt")
        with (
            patch("wa_chat_hub.channel_resolver.safe_ai_get_doc", return_value=account),
            patch(
                "wa_chat_hub.channel_resolver.get_or_create_patient_contact",
                return_value="CONTACT-0001",
            ),
            patch(
                "wa_chat_hub.channel_resolver.ensure_interakt_contact_for_reference"
            ) as ensure_contact,
            patch(
                "wa_chat_hub.channel_resolver._get_or_create_reference_conversation",
                return_value=("CONV-0001", True),
            ),
            patch(
                "wa_chat_hub.channel_resolver._conversation_department_for_account",
                return_value=None,
            ),
            patch("wa_chat_hub.identity.reconcile_conversation_identity"),
        ):
            result = get_or_create_patient_conversation_for_channel_account(
                patient,
                "Fallback Interakt",
            )

        self.assertEqual(result["conversation"], "CONV-0001")
        self.assertTrue(ensure_contact.call_args.kwargs["allow_unmapped"])
        self.assertEqual(
            ensure_contact.call_args.kwargs["pipeline_map_row"],
            {"sr_medical_department": None},
        )
