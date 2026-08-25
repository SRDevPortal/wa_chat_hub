from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wa_chat_hub.mcp.patient_records import (
    get_verified_patient_diet_charts,
    get_verified_patient_doctor_certifications,
    get_verified_patient_encounters,
    get_verified_patient_drug_prescriptions,
    get_verified_patient_profile,
    get_verified_patient_sales_invoices,
    get_verified_patient_shipping_history,
)


class TestPatientMCP(FrappeTestCase):
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_doc")
    def test_profile_rejects_unverified_conversation(self, get_doc):
        get_doc.return_value = frappe._dict(
            identity_status="Unverified",
            linked_patient="PAT-001",
            linked_reference_doctype="Patient",
            linked_reference_name="PAT-001",
        )
        with self.assertRaises(frappe.PermissionError):
            get_verified_patient_profile(patient="PAT-001", conversation="CONV-001")

    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_all")
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_doc")
    def test_encounters_are_scoped_to_verified_patient(self, get_doc, get_all):
        get_doc.return_value = frappe._dict(
            identity_status="Verified",
            linked_patient="PAT-001",
            linked_reference_doctype="Patient",
            linked_reference_name="PAT-001",
        )
        get_all.return_value = []
        get_verified_patient_encounters(patient="PAT-001", conversation="CONV-001")
        self.assertEqual(get_all.call_args.kwargs["filters"], {"patient": "PAT-001"})

    @patch("wa_chat_hub.mcp.patient_records.frappe.get_meta")
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_all")
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_doc")
    def test_drug_prescriptions_are_scoped_and_allowlisted(
        self, get_doc, get_all, get_meta
    ):
        conversation = frappe._dict(
            identity_status="Verified",
            linked_patient="PAT-001",
            linked_reference_doctype="Patient",
            linked_reference_name="PAT-001",
        )
        prescription = frappe._dict(
            medication="MED-001",
            dosage="Twice daily",
            owner="must-not-leak",
        )
        prescription.meta = frappe._dict(
            has_field=lambda fieldname: fieldname in prescription
        )
        encounter = frappe._dict(
            name="ENC-001",
            encounter_date="2026-08-25",
            practitioner_name="DR-001",
            sr_pe_instruction="After food",
            drug_prescription=[prescription],
            sr_homeopathy_drug_prescription=[],
            sr_allopathy_drug_prescription=[],
        )
        get_doc.side_effect = [conversation, encounter]
        get_all.return_value = ["ENC-001"]
        get_meta.return_value.has_field.return_value = True

        result = get_verified_patient_drug_prescriptions(
            patient="PAT-001", conversation="CONV-001"
        )

        self.assertEqual(
            get_all.call_args.kwargs["filters"],
            {"patient": "PAT-001", "docstatus": ["!=", 2]},
        )
        self.assertEqual(result["drug_prescription"][0]["medication"], "MED-001")
        self.assertNotIn("owner", result["drug_prescription"][0])

    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_doc")
    def test_drug_prescriptions_reject_wrong_patient(self, get_doc):
        get_doc.return_value = frappe._dict(
            identity_status="Verified",
            linked_patient="PAT-001",
            linked_reference_doctype="Patient",
            linked_reference_name="PAT-001",
        )
        with self.assertRaises(frappe.PermissionError):
            get_verified_patient_drug_prescriptions(
                patient="PAT-002", conversation="CONV-001"
            )

    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_all")
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_doc")
    def test_diet_charts_are_resolved_only_through_patient_encounters(self, get_doc, get_all):
        get_doc.return_value = frappe._dict(
            identity_status="Verified",
            linked_patient="PAT-001",
            linked_reference_doctype="Patient",
            linked_reference_name="PAT-001",
        )
        get_all.return_value = []
        get_verified_patient_diet_charts(patient="PAT-001", conversation="CONV-001")
        self.assertEqual(
            get_all.call_args.kwargs["filters"],
            {"patient": "PAT-001", "diet_chart": ["is", "set"]},
        )

    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_all")
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_doc")
    def test_sales_invoices_are_scoped_to_verified_patient(self, get_doc, get_all):
        get_doc.return_value = frappe._dict(
            identity_status="Verified",
            linked_patient="PAT-001",
            linked_reference_doctype="Patient",
            linked_reference_name="PAT-001",
        )
        get_all.return_value = []
        get_verified_patient_sales_invoices(patient="PAT-001", conversation="CONV-001")
        self.assertEqual(
            get_all.call_args.kwargs["filters"],
            {"patient": "PAT-001", "docstatus": ["!=", 2]},
        )

    @patch("wa_chat_hub.mcp.patient_records.frappe.get_meta")
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_all")
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_doc")
    def test_doctor_certifications_start_from_patient_encounters(self, get_doc, get_all, get_meta):
        get_doc.return_value = frappe._dict(
            identity_status="Verified",
            linked_patient="PAT-001",
            linked_reference_doctype="Patient",
            linked_reference_name="PAT-001",
        )
        get_all.return_value = []
        get_meta.return_value.has_field.return_value = True
        get_verified_patient_doctor_certifications(
            patient="PAT-001", conversation="CONV-001"
        )
        self.assertEqual(get_all.call_args.kwargs["filters"], {"patient": "PAT-001"})

    @patch("wa_chat_hub.mcp.patient_records.frappe.get_meta")
    @patch("wa_chat_hub.mcp.patient_records.frappe.db.exists")
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_all")
    @patch("wa_chat_hub.mcp.patient_records.safe_ai_get_doc")
    def test_shipping_history_is_scoped_to_verified_patient(
        self, get_doc, get_all, db_exists, get_meta
    ):
        get_doc.return_value = frappe._dict(
            identity_status="Verified",
            linked_patient="PAT-001",
            linked_reference_doctype="Patient",
            linked_reference_name="PAT-001",
        )
        get_all.return_value = []
        db_exists.return_value = True
        get_meta.return_value.has_field.return_value = True

        get_verified_patient_shipping_history(patient="PAT-001", conversation="CONV-001")

        self.assertEqual(
            get_all.call_args_list[0].kwargs["filters"],
            {"patient": "PAT-001"},
        )
