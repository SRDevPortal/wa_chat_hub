"""Read-only navigation from ERP records to existing WhatsApp conversations."""

import frappe
from frappe import _

from wa_chat_hub.permissions import filter_accessible_conversation_rows
from wa_chat_hub.phone_normalization import normalize_phone

LIMIT = 50
PHONE_FIELDS = (
    "mobile_no",
    "phone",
    "mobile",
    "whatsapp_no",
    "whatsapp_number",
    "custom_whatsapp_number",
)


def _phones(doc):
    # Exact international digits only; never equate different country codes by suffix.
    return {
        normalize_phone(doc.get(field)) for field in PHONE_FIELDS if doc.get(field)
    } - {""}


def _related_records(doc):
    references = {(doc.doctype, doc.name)}
    if doc.doctype == "Customer":
        if doc.get("lead_name"):
            references.add(("Lead", doc.lead_name))
        leads = (
            frappe.get_list(
                "Lead",
                filters={"customer": doc.name},
                pluck="name",
                limit_page_length=LIMIT,
            )
            if frappe.has_permission("Lead", "read")
            else []
        )
        references.update(("Lead", name) for name in leads)
    elif doc.doctype == "Lead":
        if doc.get("customer"):
            references.add(("Customer", doc.customer))
        customers = (
            frappe.get_list(
                "Customer",
                filters={"lead_name": doc.name},
                pluck="name",
                limit_page_length=LIMIT,
            )
            if frappe.has_permission("Customer", "read")
            else []
        )
        references.update(("Customer", name) for name in customers)
    result = [doc]
    for doctype, name in sorted(references - {(doc.doctype, doc.name)}):
        if frappe.db.exists(doctype, name):
            related = frappe.get_doc(doctype, name)
            if related.has_permission("read"):
                result.append(related)
    return result


def _conversations(filters):
    rows = frappe.get_list(
        "Chat Conversation",
        filters=filters,
        fields=[
            "name",
            "channel_account",
            "status",
            "last_message_time",
            "linked_crm_lead",
            "linked_reference_doctype",
            "linked_reference_name",
        ],
        order_by="last_message_time desc, modified desc",
        limit_page_length=LIMIT + 1,
    )
    return filter_accessible_conversation_rows(rows)


@frappe.whitelist()
def find_chats(reference_doctype, reference_name):
    if reference_doctype not in {"Lead", "Customer", "CRM Lead"}:
        frappe.throw(_("Select a Lead or Customer record."))
    doc = frappe.get_doc(reference_doctype, reference_name)
    doc.check_permission("read")
    related = _related_records(doc)
    matches = {}
    by_doctype = {}
    for record in related:
        by_doctype.setdefault(record.doctype, []).append(record.name)
    for doctype, names in by_doctype.items():
        for row in _conversations(
            {
                "linked_reference_doctype": doctype,
                "linked_reference_name": ["in", names],
            }
        ):
            matches[row.name] = row
        if doctype == "CRM Lead":
            for row in _conversations({"linked_crm_lead": ["in", names]}):
                matches[row.name] = row
    if not matches:
        phones = set().union(*(_phones(record) for record in related))
        if doc.doctype == "Customer" and doc.get("customer_primary_contact"):
            contact = frappe.get_doc("Contact", doc.customer_primary_contact)
            if contact.has_permission("read"):
                phones.update(_phones(contact))
                phones.update(
                    normalize_phone(row.phone)
                    for row in contact.get("phone_nos", [])
                    if row.phone
                )
        if phones:
            contacts = frappe.get_list(
                "Chat Contact",
                filters={"phone_number": ["in", sorted(phones)]},
                pluck="name",
                limit_page_length=LIMIT + 1,
            )
            if contacts:
                for row in _conversations({"contact": ["in", contacts]}):
                    matches[row.name] = row
    rows = sorted(
        matches.values(),
        key=lambda row: (str(row.last_message_time or ""), str(row.name)),
        reverse=True,
    )
    return {
        "conversations": [
            {
                key: row.get(key)
                for key in ("name", "channel_account", "status", "last_message_time")
            }
            for row in rows[:LIMIT]
        ],
        "has_more": len(rows) > LIMIT,
        "message": _(
            "No accessible WhatsApp conversation found. Check the phone number, including country code."
        ),
    }
