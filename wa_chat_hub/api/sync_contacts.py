import frappe
import re

def add_schema():
    frappe.set_user("Administrator")
    doc = frappe.get_doc("DocType", "Chat Contact")
    # Quick schema append logic natively
    fields = [f.fieldname for f in doc.fields]
    changed = False
    
    if "source_doctype" not in fields:
        doc.append("fields", {
            "fieldname": "source_doctype",
            "fieldtype": "Data",
            "label": "Source DocType"
        })
        changed = True
        
    if "source_name" not in fields:
        doc.append("fields", {
            "fieldname": "source_name",
            "fieldtype": "Data",
            "label": "Source Name"
        })
        changed = True
        
    if changed:
        doc.save(ignore_permissions=True)
        frappe.db.commit()

def sanitize_phone(phone):
    if not phone: return None
    p = re.sub(r'\D', '', str(phone))
    if len(p) >= 10:
        return p
    return None

@frappe.whitelist()
def sync_all():
    add_schema()
    
    synced = 0
    frappe.set_user("Administrator")
    
    # 1. Customers
    if frappe.db.exists("DocType", "Customer"):
        meta = frappe.get_meta("Customer")
        cust_fields = ["name", "customer_name"]
        if meta.has_field("mobile_no"): cust_fields.append("mobile_no")
        if meta.has_field("custom_whatsapp_number"): cust_fields.append("custom_whatsapp_number")
        
        cs = frappe.get_all("Customer", fields=cust_fields)
        for c in cs:
            p = sanitize_phone(c.get("custom_whatsapp_number") or c.get("mobile_no"))
            if p:
                synced += upsert_contact(p, c.customer_name, "Customer", c.name)

    # 2. Leads (try CRM Lead and Lead)
    for dt in ["Lead", "CRM Lead"]:
        if frappe.db.exists("DocType", dt):
            lead_fields = ["name"]
            meta = frappe.get_meta(dt)
            if meta.has_field("mobile_no"): lead_fields.append("mobile_no")
            if meta.has_field("phone"): lead_fields.append("phone")
            if meta.has_field("lead_name"): lead_fields.append("lead_name")
            if meta.has_field("first_name"): lead_fields.append("first_name")
            
            ls = frappe.get_all(dt, fields=lead_fields)
            for l in ls:
                raw_phone = l.get("mobile_no") or l.get("phone")
                p = sanitize_phone(raw_phone)
                name = l.get("lead_name") or l.get("first_name") or l.name
                if p:
                    synced += upsert_contact(p, name, dt, l.name)

    return {"message": f"Successfully synced {synced} contacts!"}

def upsert_contact(phone, name, source_dt, source_nm):
    try:
        if frappe.db.exists("Chat Contact", phone):
            # Already exists (because autoname is phone_number format)
            return 0
            
        contact = frappe.new_doc("Chat Contact")
        contact.phone_number = phone
        contact.display_name = name or phone
        if hasattr(contact, "source_doctype"):
            contact.source_doctype = source_dt
            contact.source_name = source_nm
        contact.insert(ignore_permissions=True)
        frappe.db.commit()
        return 1
    except Exception as e:
        frappe.log_error(f"Failed to sync {phone}: {str(e)}", "WA Contact Sync Error")
    return 0

def run():
    res = sync_all()
    print(res["message"])
