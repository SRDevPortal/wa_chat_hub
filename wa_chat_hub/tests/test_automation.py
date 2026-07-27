from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wa_chat_hub.automation import send_patient_template


class TestAutomatedPatientTemplate(FrappeTestCase):
    def test_routes_sends_and_persists_template(self):
        patient = frappe._dict(name="PAT-0001")
        account = frappe._dict(name="Interakt Test", is_active=1, channel_type="Interakt")
        contact = frappe._dict(name="CONTACT-0001", phone_number="919876543210")

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
                "wa_chat_hub.automation.get_or_create_mapped_patient_conversation",
                return_value={
                    "conversation": "CONV-0001",
                    "channel_account": "Interakt Test",
                    "contact": "CONTACT-0001",
                },
            ),
            patch(
                "wa_chat_hub.automation.send_interakt_template_message",
                return_value={
                    "sent": True,
                    "delivery_status": "Sent",
                    "provider_message_id": "MSG-0001",
                },
            ) as send_template,
            patch(
                "wa_chat_hub.automation.append_message",
                return_value={"message": "CHAT-MSG-0001"},
            ) as append_message,
            patch("wa_chat_hub.automation.frappe.set_user"),
        ):
            result = send_patient_template(
                patient="PAT-0001",
                template_name="order_picked_up",
                language_code="en",
                body_values=["Patient", "ORDER-0001"],
                event_key="shipment:SHIP-0001:order_picked_up",
            )

        self.assertEqual(result["message"], "CHAT-MSG-0001")
        self.assertEqual(result["provider_message_id"], "MSG-0001")
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
