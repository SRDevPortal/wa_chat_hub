import frappe
import re

from wa_chat_hub.security import (
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_insert,
    set_service_user_context,
)


def add_schema():
    # Schema changes must be handled by patches, not by the WhatsApp service user.
    meta = frappe.get_meta("Chat Contact")
    missing = [field for field in ("source_doctype", "source_name") if not meta.has_field(field)]
    if missing:
        frappe.log_error(
            f"Chat Contact source fields missing: {', '.join(missing)}",
            "WA Contact Sync Schema Missing",
        )

def sanitize_phone(phone):
    if not phone: return None
    p = re.sub(r'\D', '', str(phone))
    if len(p) >= 10:
        return p
    return None

@frappe.whitelist()
def sync_all():
    set_service_user_context("contact_sync")
    add_schema()
    
    synced = 0
    
    # 1. Customers
    if safe_ai_exists("DocType", "Customer"):
        meta = frappe.get_meta("Customer")
        cust_fields = ["name", "customer_name"]
        if meta.has_field("mobile_no"): cust_fields.append("mobile_no")
        if meta.has_field("custom_whatsapp_number"): cust_fields.append("custom_whatsapp_number")
        
        cs = safe_ai_get_all("Customer", fields=cust_fields)
        for c in cs:
            p = sanitize_phone(c.get("custom_whatsapp_number") or c.get("mobile_no"))
            if p:
                synced += upsert_contact(p, c.customer_name, "Customer", c.name)

    # 2. Leads (try CRM Lead and Lead)
    for dt in ["Lead", "CRM Lead"]:
        if safe_ai_exists("DocType", dt):
            lead_fields = ["name"]
            meta = frappe.get_meta(dt)
            if meta.has_field("mobile_no"): lead_fields.append("mobile_no")
            if meta.has_field("phone"): lead_fields.append("phone")
            if meta.has_field("lead_name"): lead_fields.append("lead_name")
            if meta.has_field("first_name"): lead_fields.append("first_name")
            
            ls = safe_ai_get_all(dt, fields=lead_fields)
            for l in ls:
                raw_phone = l.get("mobile_no") or l.get("phone")
                p = sanitize_phone(raw_phone)
                name = l.get("lead_name") or l.get("first_name") or l.name
                if p:
                    synced += upsert_contact(p, name, dt, l.name)

    return {"message": f"Successfully synced {synced} contacts!"}

def upsert_contact(phone, name, source_dt, source_nm):
    try:
        if safe_ai_exists("Chat Contact", phone):
            # Already exists (because autoname is phone_number format)
            return 0
            
        contact = frappe.new_doc("Chat Contact")
        contact.phone_number = phone
        contact.display_name = name or phone
        if hasattr(contact, "source_doctype"):
            contact.source_doctype = source_dt
            contact.source_name = source_nm
        safe_ai_insert(contact)
        frappe.db.commit()
        return 1
    except Exception as e:
        frappe.log_error(f"Failed to sync {phone}: {str(e)}", "WA Contact Sync Error")
    return 0

def run():
    res = sync_all()
    print(res["message"])
