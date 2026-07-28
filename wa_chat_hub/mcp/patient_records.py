from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, getdate, nowdate, nowtime

from wa_chat_hub.security import assert_ai_doctype_permission, safe_ai_get_all, safe_ai_get_doc


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
    patient_name = _verified_patient(patient=patient, conversation=conversation)
    doc = safe_ai_get_doc("Patient", patient_name)
    return {"patient": _allowlisted_doc(doc, PATIENT_FIELDS)}


def get_verified_patient_encounters(
    *, patient: str, conversation: str, limit: int = 5
) -> dict[str, Any]:
    """Return recent encounters belonging only to the verified conversation patient."""
    patient_name = _verified_patient(patient=patient, conversation=conversation)
    rows = safe_ai_get_all(
        "Patient Encounter",
        filters={"patient": patient_name},
        fields=list(ENCOUNTER_FIELDS),
        order_by="encounter_date desc, encounter_time desc, modified desc",
        limit_page_length=_safe_limit(limit),
    )
    return {"patient": patient_name, "encounters": [dict(row) for row in rows]}


def get_verified_patient_diet_charts(
    *, patient: str, conversation: str, limit: int = 5
) -> dict[str, Any]:
    """Return diet charts linked from the verified patient's recent encounters."""
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


def create_verified_patient_draft_encounter(
    *,
    patient: str | None = None,
    conversation: str,
    encounter_reason: str,
    customer_confirmed: int = 0,
    encounter_type: str = "Followup",
    encounter_place: str = "Online",
    complaints: str | None = None,
    observations: str | None = None,
    investigations: str | None = None,
    diagnosis: str | None = None,
    notes: str | None = None,
    instructions: str | None = None,
    practitioner: str | None = None,
    appointment: str | None = None,
    diet_chart: str | None = None,
    medical_department: str | None = None,
    company: str | None = None,
    customer_address: str | None = None,
    encounter_date: str | None = None,
    encounter_time: str | None = None,
) -> dict[str, Any]:
    """Create a draft Patient Encounter for the current chat patient or lead."""
    if not cint(customer_confirmed):
        frappe.throw(
            _("Customer confirmation is required before creating a draft encounter."),
            frappe.PermissionError,
        )

    encounter_reason = str(encounter_reason or "").strip()
    if not encounter_reason:
        frappe.throw(_("encounter_reason is required."))

    assert_ai_doctype_permission("Patient Encounter", "write")
    company = _existing_link_or_default("Company", company)
    if not company:
        frappe.throw(_("Company is required to create a Patient Encounter."))

    patient_name = _patient_for_draft_encounter(
        patient=patient,
        conversation=conversation,
        company=company,
    )
    patient_doc = safe_ai_get_doc("Patient", patient_name)

    encounter_type = _select_value(encounter_type, {"Followup", "Order", "Appointment"}, "Followup")
    encounter_place = _select_value(encounter_place, {"Online", "OPD"}, "Online")

    medical_department = (
        _existing_link_or_none("Medical Department", medical_department)
        or _existing_link_or_none("Medical Department", patient_doc.get("sr_medical_department"))
    )
    practitioner = _existing_link_or_none("Healthcare Practitioner", practitioner)
    appointment = _existing_link_or_none("Patient Appointment", appointment)
    diet_chart = _existing_link_or_none("Diet Chart", diet_chart)

    doc = frappe.new_doc("Patient Encounter")
    _set_if(doc, "naming_series", "HLC-ENC-.YYYY.-")
    _set_if(doc, "sr_encounter_type", encounter_type)
    _set_if(doc, "sr_encounter_place", encounter_place)
    _set_if(doc, "patient", patient_name)
    _set_if(doc, "patient_name", patient_doc.get("patient_name"))
    _set_if(doc, "patient_sex", patient_doc.get("sex"))
    _set_if(doc, "patient_age", patient_doc.get("sr_patient_age"))
    _set_if(doc, "sr_pe_mobile", patient_doc.get("mobile"))
    _set_if(doc, "sr_pe_deptt", medical_department)
    _set_if(doc, "sr_pe_age", patient_doc.get("sr_patient_age"))
    _set_if(doc, "company", company)
    _set_if(doc, "status", "Open")
    _set_if(doc, "encounter_date", _safe_date(encounter_date))
    _set_if(doc, "encounter_time", str(encounter_time or nowtime()))
    _set_if(doc, "medical_department", medical_department)
    _set_if(doc, "practitioner", practitioner)
    _set_if(doc, "pe_practitioner", practitioner)
    if practitioner:
        practitioner_name = frappe.db.get_value(
            "Healthcare Practitioner", practitioner, "practitioner_name"
        )
        _set_if(doc, "practitioner_name", practitioner_name or practitioner)
    _set_if(doc, "appointment", appointment)
    _set_if(doc, "diet_chart", diet_chart)
    _set_if(doc, "sr_encounter_source", _existing_link_or_none("SR Lead Source", "WhatsApp"))
    _set_if(doc, "sr_sales_type", _existing_link_or_none("SR Sales Type", "Whatsapp"))
    _set_if(doc, "sr_complaints", complaints or encounter_reason)
    _set_if(doc, "sr_observations", observations)
    _set_if(doc, "sr_investigations", investigations)
    _set_if(doc, "sr_diagnosis", diagnosis)
    encounter_notes = notes or f"Draft encounter created from WhatsApp chat: {encounter_reason}"
    customer_address = str(customer_address or "").strip()
    if customer_address:
        encounter_notes = f"{encounter_notes}\nCustomer address: {customer_address}"
    _set_if(doc, "sr_notes", encounter_notes)
    _set_if(doc, "sr_pe_instruction", instructions)

    doc.insert(ignore_permissions=True, ignore_links=True)
    return {
        "success": True,
        "patient": patient_name,
        "patient_encounter": doc.name,
        "docstatus": doc.docstatus,
        "status": doc.get("status"),
        "encounter_date": doc.get("encounter_date"),
        "encounter_time": doc.get("encounter_time"),
        "message": "Draft Patient Encounter created.",
    }


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


def _patient_for_draft_encounter(
    *,
    patient: str | None,
    conversation: str,
    company: str | None = None,
) -> str:
    """Resolve a patient for draft encounter creation.

    Read-only patient-record MCPs stay verified-patient only. Draft encounter creation is
    allowed for any chat where this MCP is explicitly exposed in Allowed MCP Tool Names:
    - verified/linked Patient chats use that Patient;
    - Lead/CRM Lead chats create or reuse a minimal Patient from the chat contact/lead.
    """
    patient = str(patient or "").strip()
    conversation = str(conversation or "").strip()
    if not conversation:
        frappe.throw(_("Conversation context is required."), frappe.PermissionError)

    convo = safe_ai_get_doc("Chat Conversation", conversation)
    linked_patient = (
        getattr(convo, "linked_patient", None)
        or (
            getattr(convo, "linked_reference_name", None)
            if getattr(convo, "linked_reference_doctype", None) == "Patient"
            else None
        )
    )
    if linked_patient:
        if patient and patient != linked_patient:
            frappe.throw(
                _("Patient does not match this conversation."),
                frappe.PermissionError,
            )
        return linked_patient
    if patient:
        frappe.throw(
            _("Patient does not match this conversation."),
            frappe.PermissionError,
        )

    return _create_or_get_patient_from_conversation(convo, company=company)


def _create_or_get_patient_from_conversation(convo, company: str | None = None) -> str:
    contact = getattr(convo, "contact", None)
    phone_number = ""
    if contact and frappe.db.exists("Chat Contact", contact):
        phone_number = frappe.db.get_value("Chat Contact", contact, "phone_number") or contact

    mobile_last10 = _last10(phone_number)
    if mobile_last10:
        existing_patient = frappe.db.get_value(
            "Patient",
            {
                "mobile": ["like", f"%{mobile_last10}%"],
            },
            "name",
        )
        if existing_patient:
            return existing_patient

    lead_name = getattr(convo, "linked_crm_lead", None)
    first_name = ""
    department = getattr(convo, "department", None)
    disease = None
    if lead_name and frappe.db.exists("CRM Lead", lead_name):
        lead = safe_ai_get_doc("CRM Lead", lead_name)
        first_name = (
            lead.get("first_name")
            or lead.get("lead_name")
            or lead.get("name")
            or ""
        )
        phone_number = phone_number or lead.get("mobile_no") or lead.get("phone") or ""
        department = department or lead.get("sr_medical_department") or lead.get("department")
        disease = lead.get("sr_lead_disease")
    elif lead_name and frappe.db.exists("Lead", lead_name):
        lead = safe_ai_get_doc("Lead", lead_name)
        first_name = (
            lead.get("first_name")
            or lead.get("lead_name")
            or lead.get("name")
            or ""
        )
        phone_number = phone_number or lead.get("mobile_no") or lead.get("phone") or ""

    first_name = str(first_name or "").strip() or f"WhatsApp {mobile_last10 or 'Lead'}"
    mobile = _last10(phone_number) or str(phone_number or "").strip()
    if not mobile:
        frappe.throw(_("Mobile number is required to create a draft encounter patient."))

    assert_ai_doctype_permission("Patient", "write")
    company = _existing_link_or_default("Company", company)
    if not company:
        frappe.throw(_("Company is required to create a draft encounter patient."))

    patient_doc = frappe.new_doc("Patient")
    _set_if(patient_doc, "first_name", first_name)
    _set_if(patient_doc, "patient_name", first_name)
    _set_if(patient_doc, "mobile", mobile)
    _set_if(patient_doc, "company", company)
    _set_if(patient_doc, "status", "Active")
    _set_if(patient_doc, "sex", "Male")
    _set_if(patient_doc, "sr_medical_department", _existing_link_or_none("Medical Department", department))
    _set_if(patient_doc, "sr_dpt_disease", disease)
    _set_if(patient_doc, "created_by_agent", "WA MCP Draft Encounter")
    patient_doc.insert(ignore_permissions=True, ignore_links=True)

    if getattr(convo, "name", None):
        try:
            frappe.db.set_value("Chat Conversation", convo.name, "linked_patient", patient_doc.name)
            frappe.db.set_value("Chat Conversation", convo.name, "party_type", "Patient")
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
    if contact and frappe.db.exists("Chat Contact", contact):
        try:
            frappe.db.set_value("Chat Contact", contact, "linked_patient", patient_doc.name)
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()

    return patient_doc.name


def _last10(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _allowlisted_doc(doc, fields: tuple[str, ...]) -> dict[str, Any]:
    return {fieldname: doc.get(fieldname) for fieldname in fields if doc.meta.has_field(fieldname) or fieldname == "name"}


def _existing_fields(doctype: str, fields: tuple[str, ...]) -> list[str]:
    meta = frappe.get_meta(doctype)
    return [
        fieldname
        for fieldname in fields
        if fieldname == "name" or meta.has_field(fieldname)
    ]


def _existing_link_or_none(doctype: str, value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value or not frappe.db.exists("DocType", doctype):
        return None
    return value if frappe.db.exists(doctype, value) else None


def _existing_link_or_default(doctype: str, value: str | None) -> str | None:
    existing = _existing_link_or_none(doctype, value)
    if existing:
        return existing
    rows = frappe.get_all(doctype, pluck="name", limit_page_length=1) if frappe.db.exists("DocType", doctype) else []
    return rows[0] if rows else None


def _safe_date(value: str | None):
    if not value:
        return nowdate()
    try:
        return getdate(value)
    except Exception:
        return nowdate()


def _select_value(value: str | None, allowed: set[str], default: str) -> str:
    value = str(value or "").strip()
    return value if value in allowed else default


def _set_if(doc, fieldname: str, value) -> None:
    if value is not None and (fieldname == "name" or doc.meta.has_field(fieldname)):
        doc.set(fieldname, value)


def _safe_limit(value: int) -> int:
    return max(1, min(10, cint(value or 5)))
