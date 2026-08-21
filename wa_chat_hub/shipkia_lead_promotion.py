from __future__ import annotations

import frappe


SHIPKIA_COPY_FIELDS = (
    "whatsapp_no",
    "shipkia_business_type",
    "shipkia_business_name",
    "shipkia_product_category",
    "shipkia_primary_sales_channel",
    "shipkia_website_store_url",
    "shipkia_order_source",
    "shipkia_lead_source",
    "shipkia_first_contact_channel",
    "shipkia_campaign_name",
    "shipkia_utm_source",
    "shipkia_referrer_name",
    "shipkia_source_notes",
    "shipkia_monthly_shipments",
    "shipkia_pickup_city",
    "shipkia_pickup_pincode",
    "shipkia_delivery_city",
    "shipkia_delivery_scope",
    "shipkia_average_weight",
    "shipkia_package_dimensions",
    "shipkia_payment_mode",
    "shipkia_preferred_shipping_mode",
    "shipkia_requirement_details",
    "shipkia_current_aggregator_status",
    "shipkia_current_aggregator_name",
    "shipkia_current_shipping_rate",
    "shipkia_rto_percentage",
    "shipkia_qualification_status",
    "shipkia_lead_temperature",
    "shipkia_next_follow_up",
    "shipkia_callback_required",
    "shipkia_context_completed",
    "shipkia_context_updated_at",
    "shipkia_followup_notes",
    "shipkia_objections",
    "shipkia_sales_stage",
    "shipkia_rate_shared",
    "shipkia_signup_link_sent",
    "shipkia_account_created",
    "shipkia_first_shipment_done",
    "shipkia_converted_on",
    "shipkia_conversion_notes",
)


def sync_qualified_lead_to_crm(doc, method: str | None = None) -> None:
    if not frappe.db.exists("DocType", "CRM Lead"):
        return
    if getattr(doc, "shipkia_qualification_status", None) != "Qualified":
        return

    lead_meta = frappe.get_meta("Lead")
    crm_meta = frappe.get_meta("CRM Lead")
    phone = getattr(doc, "mobile_no", None) or getattr(doc, "phone", None)
    existing = None
    if phone and crm_meta.has_field("mobile_no"):
        existing = frappe.db.get_value("CRM Lead", {"mobile_no": phone}, "name")
    if not existing and getattr(doc, "whatsapp_no", None) and crm_meta.has_field("whatsapp_no"):
        existing = frappe.db.get_value("CRM Lead", {"whatsapp_no": doc.whatsapp_no}, "name")

    crm_doc = frappe.get_doc("CRM Lead", existing) if existing else frappe.new_doc("CRM Lead")
    if crm_meta.has_field("lead_name"):
        crm_doc.lead_name = getattr(doc, "lead_name", None) or getattr(doc, "first_name", None) or phone or doc.name
    if crm_meta.has_field("first_name"):
        crm_doc.first_name = getattr(doc, "first_name", None) or getattr(doc, "lead_name", None) or phone or doc.name
    if phone and crm_meta.has_field("mobile_no"):
        crm_doc.mobile_no = phone
    if crm_meta.has_field("status") and frappe.db.exists("CRM Lead Status", "Qualified"):
        crm_doc.status = "Qualified"

    for fieldname in SHIPKIA_COPY_FIELDS:
        if lead_meta.has_field(fieldname) and crm_meta.has_field(fieldname):
            crm_doc.set(fieldname, doc.get(fieldname))

    if crm_doc.is_new():
        crm_doc.insert(ignore_permissions=True)
    else:
        crm_doc.save(ignore_permissions=True)
