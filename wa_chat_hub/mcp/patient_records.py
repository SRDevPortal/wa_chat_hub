from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint

from wa_chat_hub.security import assert_ai_doctype_permission, safe_ai_get_all, safe_ai_get_doc


def _patient_flow_disabled() -> dict[str, Any]:
    return {
        "success": False,
        "reason": "patient_flow_disabled",
        "message": "Patient tools are disabled for ShipKia customer flow.",
    }


PATIENT_FIELDS = (
    "name",
    "patient_name",
    "sex",
    "sr_patient_age",
    "sr_medical_department",
    "sr_dpt_disease",
    "sr_dpt_language",
    "status",
    "allergies",
)

ENCOUNTER_FIELDS = (
    "name",
    "encounter_date",
    "encounter_time",
    "status",
    "sr_encounter_type",
    "sr_encounter_place",
    "practitioner_name",
    "medical_department",
    "sr_complaints",
    "sr_observations",
    "sr_investigations",
    "sr_diagnosis",
    "sr_notes",
    "sr_pe_instruction",
    "diet_chart",
)

DRUG_PRESCRIPTION_TABLES = (
    "drug_prescription",
    "sr_homeopathy_drug_prescription",
    "sr_allopathy_drug_prescription",
)

DRUG_PRESCRIPTION_FIELDS = (
    "medication",
    "drug_code",
    "drug_name",
    "sr_medication_name_print",
    "strength",
    "strength_uom",
    "dosage_form",
    "dosage_by_interval",
    "dosage",
    "interval",
    "interval_uom",
    "period",
    "number_of_repeats_allowed",
    "sr_drug_instruction",
    "comment",
)

DIET_CHART_FIELDS = (
    "name",
    "diet_chart_name",
    "instructions",
    "allowed_foods",
    "restricted_foods",
)

SALES_INVOICE_FIELDS = (
    "name",
    "posting_date",
    "due_date",
    "status",
    "currency",
    "grand_total",
    "rounded_total",
    "paid_amount",
    "outstanding_amount",
    "is_return",
    "return_against",
)

SALES_INVOICE_ITEM_FIELDS = (
    "item_code",
    "item_name",
    "description",
    "qty",
    "uom",
    "rate",
    "amount",
)

PRACTITIONER_FIELDS = (
    "name",
    "practitioner_name",
    "status",
    "sr_reg_no",
    "sr_qualification",
    "sr_college_university",
    "department",
    "designation",
    "sr_pathy",
    "hospital",
)

SHIPMENT_FIELDS = (
    "name",
    "sales_invoice",
    "patient_encounter",
    "patient",
    "customer",
    "shipkia_order_id",
    "shipkia_awb_number",
    "shipkia_tracking_id",
    "shipkia_stage",
    "shipkia_status",
    "normalized_status",
    "shipkia_status_detail",
    "payment_mode",
    "delivery_partner",
    "delivery_location",
    "company",
    "company_id",
    "shipkia_estimated_delivery",
    "shipkia_delivered_on",
    "last_synced_on",
)

SHIPMENT_EVENT_FIELDS = (
    "date_time",
    "status",
    "detail",
    "location",
)

ENCOUNTER_SHIPMENT_FIELDS = (
    "name",
    "encounter_date",
    "encounter_time",
    "pe_shipkia_order_id",
    "pe_shipkia_awb_number",
    "pe_shipkia_stage",
    "pe_shipkia_status",
    "pe_shipkia_estimated_delivery",
    "pe_shipkia_delivered_on",
    "pe_delivery_partner",
    "pe_shipkia_shipment",
)


def get_verified_patient_profile(*, patient: str, conversation: str) -> dict[str, Any]:
    """Return a small allowlisted profile for the verified conversation patient."""
    return _patient_flow_disabled()

    patient_name = _verified_patient(patient=patient, conversation=conversation)
    doc = safe_ai_get_doc("Patient", patient_name)
    return {"patient": _allowlisted_doc(doc, PATIENT_FIELDS)}


def get_verified_patient_encounters(
    *, patient: str, conversation: str, limit: int = 5
) -> dict[str, Any]:
    """Return recent encounters belonging only to the verified conversation patient."""
    return _patient_flow_disabled()

    patient_name = _verified_patient(patient=patient, conversation=conversation)
    rows = safe_ai_get_all(
        "Patient Encounter",
        filters={"patient": patient_name},
        fields=list(ENCOUNTER_FIELDS),
        order_by="encounter_date desc, encounter_time desc, modified desc",
        limit_page_length=_safe_limit(limit),
    )
    return {"patient": patient_name, "encounters": [dict(row) for row in rows]}


def get_verified_patient_drug_prescriptions(
    *, patient: str, conversation: str, limit: int = 10
) -> dict[str, Any]:
    """Return the latest non-cancelled encounter containing prescribed drugs."""
    return _patient_flow_disabled()

    patient_name = _verified_patient(patient=patient, conversation=conversation)
    encounter_names = safe_ai_get_all(
        "Patient Encounter",
        filters={"patient": patient_name, "docstatus": ["!=", 2]},
        pluck="name",
        order_by="encounter_date desc, encounter_time desc, modified desc",
        limit_page_length=_safe_limit(limit),
    )

    encounter_meta = frappe.get_meta("Patient Encounter")
    available_tables = [
        fieldname
        for fieldname in DRUG_PRESCRIPTION_TABLES
        if encounter_meta.has_field(fieldname)
    ]
    for encounter_name in encounter_names:
        encounter = safe_ai_get_doc("Patient Encounter", encounter_name)
        prescriptions = {
            fieldname: [
                _allowlisted_doc(row, DRUG_PRESCRIPTION_FIELDS)
                for row in (encounter.get(fieldname) or [])
            ]
            for fieldname in available_tables
        }
        if not any(prescriptions.values()):
            continue
        return {
            "patient": patient_name,
            "encounter": encounter.name,
            "encounter_date": encounter.get("encounter_date"),
            "practitioner_name": encounter.get("practitioner_name"),
            "instructions": encounter.get("sr_pe_instruction"),
            **prescriptions,
        }

    return {
        "patient": patient_name,
        "encounter": None,
        **{fieldname: [] for fieldname in available_tables},
    }


def get_verified_patient_diet_charts(
    *, patient: str, conversation: str, limit: int = 5
) -> dict[str, Any]:
    """Return diet charts linked from the verified patient's recent encounters."""
    return _patient_flow_disabled()

    patient_name = _verified_patient(patient=patient, conversation=conversation)
    encounter_rows = safe_ai_get_all(
        "Patient Encounter",
        filters={"patient": patient_name, "diet_chart": ["is", "set"]},
        fields=["name", "encounter_date", "diet_chart"],
        order_by="encounter_date desc, modified desc",
        limit_page_length=_safe_limit(limit),
    )

    charts = []
    seen = set()
    for encounter in encounter_rows:
        chart_name = encounter.get("diet_chart")
        if not chart_name or chart_name in seen:
            continue
        seen.add(chart_name)
        chart = safe_ai_get_doc("Diet Chart", chart_name)
        values = _allowlisted_doc(chart, DIET_CHART_FIELDS)
        values["patient_encounter"] = encounter.get("name")
        values["encounter_date"] = encounter.get("encounter_date")
        charts.append(values)

    return {"patient": patient_name, "diet_charts": charts}


def get_verified_patient_sales_invoices(
    *, patient: str, conversation: str, limit: int = 5
) -> dict[str, Any]:
    """Return recent invoices and allowlisted line items for the verified patient."""
    return _patient_flow_disabled()

    patient_name = _verified_patient(patient=patient, conversation=conversation)
    invoice_names = safe_ai_get_all(
        "Sales Invoice",
        filters={"patient": patient_name, "docstatus": ["!=", 2]},
        pluck="name",
        order_by="posting_date desc, posting_time desc, modified desc",
        limit_page_length=_safe_limit(limit),
    )

    invoices = []
    for invoice_name in invoice_names:
        invoice = safe_ai_get_doc("Sales Invoice", invoice_name)
        values = _allowlisted_doc(invoice, SALES_INVOICE_FIELDS)
        values["items"] = [
            _allowlisted_doc(item, SALES_INVOICE_ITEM_FIELDS)
            for item in (invoice.get("items") or [])
        ]
        invoices.append(values)
    return {"patient": patient_name, "sales_invoices": invoices}


def get_verified_patient_doctor_certifications(
    *, patient: str, conversation: str, limit: int = 10
) -> dict[str, Any]:
    """Return credentials only for practitioners linked to this patient's encounters."""
    return _patient_flow_disabled()

    patient_name = _verified_patient(patient=patient, conversation=conversation)
    practitioner_fields = [
        "practitioner",
        "pe_practitioner",
        "sr_ayurvedic_practitioner",
        "sr_homeopathy_practitioner",
        "sr_allopathy_practitioner",
    ]
    practitioner_fields = [
        fieldname
        for fieldname in practitioner_fields
        if frappe.get_meta("Patient Encounter").has_field(fieldname)
    ]
    encounters = safe_ai_get_all(
        "Patient Encounter",
        filters={"patient": patient_name},
        fields=practitioner_fields,
        order_by="encounter_date desc, encounter_time desc, modified desc",
        limit_page_length=_safe_limit(limit),
    )

    practitioner_names = []
    seen = set()
    for encounter in encounters:
        for fieldname in practitioner_fields:
            practitioner_name = encounter.get(fieldname)
            if practitioner_name and practitioner_name not in seen:
                seen.add(practitioner_name)
                practitioner_names.append(practitioner_name)

    practitioners = [
        _allowlisted_doc(
            safe_ai_get_doc("Healthcare Practitioner", practitioner_name),
            PRACTITIONER_FIELDS,
        )
        for practitioner_name in practitioner_names
    ]
    return {"patient": patient_name, "doctor_certifications": practitioners}


def get_verified_patient_shipping_history(
    *, patient: str, conversation: str, limit: int = 5
) -> dict[str, Any]:
    """Return shipment/tracking history belonging only to the verified patient."""
    return _patient_flow_disabled()

    patient_name = _verified_patient(patient=patient, conversation=conversation)
    limit = _safe_limit(limit)

    shipments = []
    if frappe.db.exists("DocType", "Shipment Tracking Shipment"):
        shipment_names = safe_ai_get_all(
            "Shipment Tracking Shipment",
            filters={"patient": patient_name},
            pluck="name",
            order_by="modified desc",
            limit_page_length=limit,
        )
        for shipment_name in shipment_names:
            shipment = safe_ai_get_doc("Shipment Tracking Shipment", shipment_name)
            values = _allowlisted_doc(shipment, SHIPMENT_FIELDS)
            values["events"] = [
                _allowlisted_doc(event, SHIPMENT_EVENT_FIELDS)
                for event in (shipment.get("events") or [])
            ]
            shipments.append(values)

    encounter_shipments = []
    encounter_fields = _existing_fields("Patient Encounter", ENCOUNTER_SHIPMENT_FIELDS)
    if len(encounter_fields) > 1:
        encounters = safe_ai_get_all(
            "Patient Encounter",
            filters={"patient": patient_name},
            fields=encounter_fields,
            order_by="encounter_date desc, encounter_time desc, modified desc",
            limit_page_length=limit,
        )
        shipment_fieldnames = set(encounter_fields) - {"name", "encounter_date", "encounter_time"}
        for encounter in encounters:
            row = dict(encounter)
            if any(row.get(fieldname) for fieldname in shipment_fieldnames):
                encounter_shipments.append(row)

    return {
        "patient": patient_name,
        "shipping_history": shipments,
        "encounter_shipping_history": encounter_shipments,
    }


def create_verified_patient_draft_encounter(**kwargs) -> dict[str, Any]:
    """Deprecated direct writer retained only to fail closed for old integrations."""
    del kwargs
    frappe.throw(
        _("Direct patient-record writes are disabled. Use a configured WA AI Workflow and MCP endpoint."),
        frappe.PermissionError,
    )


def _verified_patient(*, patient: str, conversation: str) -> str:
    patient = str(patient or "").strip()
    conversation = str(conversation or "").strip()
    if not patient or not conversation:
        frappe.throw(_("Verified patient context is required."), frappe.PermissionError)

    convo = safe_ai_get_doc("Chat Conversation", conversation)
    linked_patient = (
        getattr(convo, "linked_patient", None)
        or (
            getattr(convo, "linked_reference_name", None)
            if getattr(convo, "linked_reference_doctype", None) == "Patient"
            else None
        )
    )
    if getattr(convo, "identity_status", None) != "Verified" or linked_patient != patient:
        frappe.throw(
            _("Patient identity is not verified for this conversation."),
            frappe.PermissionError,
        )
    return patient


def _allowlisted_doc(doc, fields: tuple[str, ...]) -> dict[str, Any]:
    return {fieldname: doc.get(fieldname) for fieldname in fields if doc.meta.has_field(fieldname) or fieldname == "name"}


def _existing_fields(doctype: str, fields: tuple[str, ...]) -> list[str]:
    meta = frappe.get_meta(doctype)
    return [
        fieldname
        for fieldname in fields
        if fieldname == "name" or meta.has_field(fieldname)
    ]


def _safe_limit(value: int) -> int:
    return max(1, min(10, cint(value or 5)))
