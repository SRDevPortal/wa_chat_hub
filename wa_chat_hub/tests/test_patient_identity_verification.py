from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from wa_chat_hub.identity import (
    _matching_patient_phone_field,
    _normalized_phone,
    _phones_from_text,
    verify_patient_identity_from_inbound_message,
)


class TestPatientIdentityVerification(TestCase):
    def test_normalizes_country_code_and_formatting(self):
        self.assertEqual(_normalized_phone("+91 94660-73244"), "9466073244")

    def test_extracts_phone_but_not_short_date_parts(self):
        self.assertEqual(
            _phones_from_text("Amit\nJuly 22 1992\nMy number is +91 94660 73244"),
            {"9466073244"},
        )

    @patch("wa_chat_hub.identity.frappe")
    def test_matches_patient_mobile_or_phone(self, frappe):
        frappe.get_meta.return_value.has_field.return_value = True
        frappe.db.get_value.return_value = {
            "mobile": "9876543210",
            "phone": "+91 94660 73244",
        }

        self.assertEqual(
            _matching_patient_phone_field("HLC-PAT-2026-00001", "9876543210"),
            "mobile",
        )
        self.assertEqual(
            _matching_patient_phone_field("HLC-PAT-2026-00001", "9466073244"),
            "phone",
        )

    @patch("wa_chat_hub.identity.frappe")
    @patch("wa_chat_hub.identity._matching_patient_phone_field")
    @patch("wa_chat_hub.identity.reconcile_conversation_identity")
    @patch("wa_chat_hub.agent_router.persist_agent_route")
    @patch("wa_chat_hub.agent_router.resolve_agent_route")
    def test_verifies_only_on_three_way_match(
        self,
        resolve_route,
        persist_route,
        reconcile,
        matching_patient_phone,
        frappe,
    ):
        frappe.db.exists.return_value = True
        frappe.db.get_value.return_value = "919466073244"
        frappe.get_doc.side_effect = [
            SimpleNamespace(
                identity_status="Matched",
                linked_patient="HLC-PAT-2026-00001",
                linked_crm_lead=None,
                linked_reference_doctype="Patient",
                linked_reference_name="HLC-PAT-2026-00001",
                contact="919466073244",
            ),
            SimpleNamespace(
                conversation="1",
                direction="Inbound",
                body="Amit, my number is 9466073244",
            ),
        ]
        matching_patient_phone.return_value = "mobile"
        reconcile.return_value = {"changed": True}
        resolve_route.return_value = SimpleNamespace(agent_profile="Patient Care Agent")

        result = verify_patient_identity_from_inbound_message("1", "1111")

        self.assertTrue(result["verified"])
        self.assertEqual(result["reason"], "three_way_phone_match")
        self.assertEqual(result["patient_phone_field"], "mobile")
        reconcile.assert_called_once_with(
            "1",
            patient="HLC-PAT-2026-00001",
            source="patient_phone_match",
            verified=True,
        )
        persist_route.assert_called_once()

    @patch("wa_chat_hub.identity.frappe")
    @patch("wa_chat_hub.identity.reconcile_conversation_identity")
    def test_does_not_verify_when_supplied_phone_differs(self, reconcile, frappe):
        frappe.db.exists.return_value = True
        frappe.db.get_value.return_value = "919466073244"
        frappe.get_doc.side_effect = [
            SimpleNamespace(
                identity_status="Matched",
                linked_patient="HLC-PAT-2026-00001",
                linked_crm_lead=None,
                linked_reference_doctype="Patient",
                linked_reference_name="HLC-PAT-2026-00001",
                contact="919466073244",
            ),
            SimpleNamespace(
                conversation="1",
                direction="Inbound",
                body="My number is 9876543210",
            ),
        ]

        result = verify_patient_identity_from_inbound_message("1", "1111")

        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "supplied_phone_mismatch")
        reconcile.assert_not_called()

    @patch("wa_chat_hub.identity.frappe")
    @patch("wa_chat_hub.identity.reconcile_conversation_identity")
    def test_does_not_verify_when_message_has_no_phone(self, reconcile, frappe):
        frappe.db.exists.return_value = True
        frappe.db.get_value.return_value = "919466073244"
        frappe.get_doc.side_effect = [
            SimpleNamespace(
                identity_status="Matched",
                linked_patient="HLC-PAT-2026-00001",
                linked_crm_lead=None,
                linked_reference_doctype="Patient",
                linked_reference_name="HLC-PAT-2026-00001",
                contact="919466073244",
            ),
            SimpleNamespace(
                conversation="1",
                direction="Inbound",
                body="My name is Amit",
            ),
        ]

        result = verify_patient_identity_from_inbound_message("1", "1111")

        self.assertFalse(result["verified"])
        self.assertEqual(result["reason"], "supplied_phone_mismatch")
        reconcile.assert_not_called()
