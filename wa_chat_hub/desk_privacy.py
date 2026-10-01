"""Project the native Conversation list after existing permission checks."""
from copy import deepcopy
import frappe
from wa_chat_hub.number_privacy import restricted, RAW_KEYS, is_safe_record_reference


def project_rows(rows):
    from privacy_shield.masking import mask_number
    from privacy_shield.display_text import mask_display
    result = deepcopy(rows)
    for row in result:
        for field, value in list(row.items()):
            if field in RAW_KEYS:
                row[field] = ""
            elif field == "contact":
                row[field] = mask_number(value)
            elif is_safe_record_reference(field, value):
                continue
            elif isinstance(value, str) and field not in ("name", "creation", "modified", "owner", "modified_by"):
                row[field] = mask_display(value)
    return result


def project_list(data):
    if isinstance(data, dict) and "keys" in data and "values" in data:
        result = deepcopy(data)
        keys = result["keys"]
        rows = project_rows([dict(zip(keys, row)) for row in result["values"]])
        result["values"] = [[row.get(key) for key in keys] for row in rows]
        return result
    return project_rows(data) if isinstance(data, list) else data


def _get(compressed):
    from privacy_shield import listing
    adapter = listing.reportview_get if compressed else listing.reportview_get_list
    result = adapter()
    if frappe.form_dict.get("doctype") == "Chat Conversation" and restricted():
        return project_list(result)
    return result


@frappe.whitelist()
def reportview_get():
    return _get(True)


@frappe.whitelist()
def reportview_get_list():
    return _get(False)


def project_document(doc):
    result = project_rows([doc])[0]
    result.pop("__onload", None)
    result["__wa_number_privacy"] = True
    return result


def _project_docs():
    # Remove originals first: Frappe serializes response.docs even on exceptions.
    documents = frappe.response.pop("docs", [])
    docinfo = frappe.response.pop("docinfo", None)
    projected = []
    for document in documents:
        as_dict = getattr(document, "as_dict", None)
        data = as_dict() if callable(as_dict) else document
        projected.append(project_document(data) if data.get("doctype") == "Chat Conversation" else data)
    if docinfo:
        from privacy_shield.history_access import scrub_docinfo
        docinfo = deepcopy(docinfo)
        scrub_docinfo(docinfo)
    frappe.response["docs"] = projected
    if docinfo is not None:
        frappe.response["docinfo"] = docinfo


def prepare_document(data, stored):
    """Restore only unchanged display projections; never accept a new Contact link."""
    result = deepcopy(data)
    visible = project_document(stored)
    for field, original in stored.items():
        if field == "contact" or field in RAW_KEYS:
            if field in result and result[field] not in (original, visible.get(field)):
                raise frappe.PermissionError("This field cannot be changed from the masked conversation form")
            result[field] = original
        elif field in result and result[field] == visible.get(field) and original != visible.get(field):
            result[field] = original
    result.pop("__wa_number_privacy", None)
    return result


@frappe.whitelist()
def getdoc(doctype, name):
    from privacy_shield.desk import getdoc as original
    result = original(doctype, name)
    if doctype == "Chat Conversation" and restricted():
        _project_docs()
    return result


@frappe.whitelist(methods=["POST", "PUT"])
def savedocs(doc, action):
    from privacy_shield.desk import savedocs as original
    data = frappe.parse_json(doc) if isinstance(doc, str) else deepcopy(doc)
    protected = data.get("doctype") == "Chat Conversation" and restricted()
    if protected:
        if data.get("__islocal") or not data.get("name"):
            raise frappe.PermissionError("Create a conversation through WA Chat Hub")
        stored = frappe.get_doc("Chat Conversation", data["name"])
        stored.check_permission("read")
        stored.check_permission("write")
        data = prepare_document(data, stored.as_dict())
    result = original(frappe.as_json(data), action)
    if protected:
        _project_docs()
    return result
