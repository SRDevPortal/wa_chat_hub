import frappe


def execute():
    doctype = "WA Knowledge Base"
    if not frappe.db.exists("DocType", doctype):
        return
    if frappe.db.count(doctype):
        frappe.log_error(
            "Skipped deleting WA Knowledge Base because records exist. Move them to WA AI Knowledge Base first.",
            "WA Knowledge Base Removal Skipped",
        )
        return

    frappe.delete_doc("DocType", doctype, ignore_permissions=True, force=True)
