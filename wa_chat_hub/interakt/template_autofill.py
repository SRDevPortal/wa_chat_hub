"""Conservative, editable suggestions from an already authorized conversation."""
from copy import deepcopy
import re
import frappe


def suggest_values(template, context):
    result = {}
    for section in ("body", "header"):
        if section == "header" and str(template.get("header_format") or "").upper() in {"IMAGE", "VIDEO", "DOCUMENT"}:
            continue
        text = template.get(section + "_preview") or ""
        previous_end = 0
        previous_field = None
        for match in re.finditer(r"\{\{\s*(\d+)\s*\}\}", text):
            before = text[previous_end:match.start()].lower().strip()[-80:]
            previous_end = match.end()
            field = None
            if re.search(r"(?:hello|hi|hey|dear|namaste|\u0928\u092e\u0938\u094d\u0924\u0947|\u092a\u094d\u0930\u093f\u092f)\s*[,!:?-]?\s*$", before) or re.search(r"(?:patient name|customer name|contact name)\s*[:?-]?\s*$", before):
                field = "contact_name"
            elif re.search(r"(?:department|\u0935\u093f\u092d\u093e\u0917)\s*[:?-]?\s*$", before):
                field = "department"
            elif re.search(r"(?:agent name|assigned agent|advisor name)\s*[:?-]?\s*$", before):
                field = "agent_name"
            elif re.search(r"(?:this is|my name is|i am|agent name|assigned agent|advisor name)\s*[:,-]?\s*$", before):
                field = "agent_name"
            elif previous_field == "agent_name" and re.fullmatch(r"\s*from\s*", before):
                field = "account_name"
            previous_field = field
            value = str(context.get(field) or "").strip() if field else ""
            # A phone-number display name must not become a browser-visible suggestion.
            if value and not re.search(r"(?:\d[\s()+.-]*){7,}", value) and "****" not in value:
                result[f"{section}_{match.group(1)}"] = value
    return result


def add_suggestions(templates, conversation):
    """Internal only: caller must authorize the conversation first."""
    doc = frappe.get_doc("Chat Conversation", conversation)
    contact = frappe.get_doc("Chat Contact", doc.contact) if doc.contact else frappe._dict()
    contact_name = contact.get("display_name")
    for doctype, link, field in (("Patient", contact.get("linked_patient"), "patient_name"), ("CRM Lead", contact.get("linked_lead"), "lead_name")):
        if link and frappe.db.exists(doctype, link):
            linked = frappe.get_doc(doctype, link)
            if frappe.has_permission(doctype, "read", doc=linked):
                contact_name = linked.get(field) or contact_name
                break
    agent = doc.get("assigned_to") or frappe.session.user
    context = {
        "contact_name": contact_name,
        "department": doc.get("department"),
        "agent_name": frappe.db.get_value("User", agent, "full_name") if agent and agent != "Guest" else "",
        "account_name": frappe.db.get_value("Chat Channel Account", doc.channel_account, "account_name") if doc.get("channel_account") else "",
    }
    rows = deepcopy(templates)
    for row in rows:
        row["autofill_values"] = suggest_values(row, context)
    return rows
