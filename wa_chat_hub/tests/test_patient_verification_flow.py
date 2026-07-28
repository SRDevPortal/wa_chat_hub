from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.patient_verification_flow import (
    AMBIGUOUS_IDENTITY_REPLY,
    VERIFY_MISMATCH_REPLY,
    VERIFY_REQUEST_REPLY,
    evaluate_patient_verification_gate,
    is_sensitive_patient_request,
)


class _Meta:
    def has_field(self, _fieldname):
        return True


class TestPatientVerificationFlow(TestCase):
    def test_detects_private_history_requests_but_not_general_questions(self):
        self.assertTrue(is_sensitive_patient_request("I want to know my medical history"))
        self.assertTrue(is_sensitive_patient_request("Mujhe meri clinical history btao"))
        self.assertFalse(is_sensitive_patient_request("What are your clinic timings?"))

    @patch("wa_chat_hub.patient_verification_flow.frappe")
    def test_sensitive_request_is_saved_before_fixed_verification_reply(self, frappe):
        frappe.get_doc.return_value = SimpleNamespace(
            party_type="Patient",
            identity_status="Matched",
            pending_patient_request=None,
            verification_started_at="2026-07-28 13:00:00",
        )
        frappe.get_meta.return_value = _Meta()

        gate = evaluate_patient_verification_gate(
            "CONV-1",
            "MSG-1",
            "I want to know my medical history",
        )

        self.assertTrue(gate.handled)
        self.assertEqual(gate.response, VERIFY_REQUEST_REPLY)
        self.assertEqual(gate.pending_request, "I want to know my medical history")
        frappe.db.set_value.assert_called_once()
        frappe.db.commit.assert_called_once()

    @patch("wa_chat_hub.patient_verification_flow.frappe")
    def test_verified_patient_resumes_saved_request(self, frappe):
        frappe.get_doc.return_value = SimpleNamespace(
            party_type="Patient",
            identity_status="Verified",
            pending_patient_request="Show my treatment history",
        )

        gate = evaluate_patient_verification_gate("CONV-1", "MSG-2", "9084553059")

        self.assertFalse(gate.handled)
        self.assertEqual(gate.pending_request, "Show my treatment history")
        self.assertEqual(gate.reason, "verified_pending_request")

    @patch("wa_chat_hub.patient_verification_flow.frappe")
    def test_unmatched_supplied_phone_gets_immediate_retry_reply(self, frappe):
        frappe.get_doc.return_value = SimpleNamespace(
            party_type="Patient",
            identity_status="Matched",
            pending_patient_request="Show my treatment history",
            verification_started_at="2026-07-28 13:00:00",
            verification_attempts=0,
        )
        frappe.get_meta.return_value = _Meta()

        gate = evaluate_patient_verification_gate("CONV-1", "MSG-2", "9999999999")

        self.assertTrue(gate.handled)
        self.assertEqual(gate.response, VERIFY_MISMATCH_REPLY)
        frappe.db.commit.assert_called_once()

    @patch("wa_chat_hub.patient_verification_flow.frappe")
    def test_ambiguous_patient_never_falls_back_to_lead_for_private_request(self, frappe):
        frappe.get_doc.return_value = SimpleNamespace(
            party_type="Patient",
            identity_status="Ambiguous",
            pending_patient_request=None,
        )

        gate = evaluate_patient_verification_gate(
            "CONV-1",
            "MSG-1",
            "Please show my medical history",
        )

        self.assertTrue(gate.handled)
        self.assertEqual(gate.response, AMBIGUOUS_IDENTITY_REPLY)
