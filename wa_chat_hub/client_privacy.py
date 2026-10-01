"""Read-only projections for generic chat APIs; preserve normal authorization."""
from copy import deepcopy
import frappe
from wa_chat_hub.number_privacy import restricted, RAW_KEYS, is_safe_record_reference
from wa_chat_hub.desk_privacy import project_document

TARGETS = frozenset({"Chat Contact", "Chat Conversation"})


def project_read(data, doctype):
    from privacy_shield.display_text import mask_display
    from privacy_shield.masking import mask_number
    if isinstance(data, (list, tuple)):
        return [project_read(value, doctype) for value in data]
    if isinstance(data, dict):
        result = deepcopy(data)
        for key, value in list(result.items()):
            if key in RAW_KEYS:
                result[key] = ""
            elif key == "contact" or (doctype == "Chat Contact" and key in ("name", "phone_number")):
                result[key] = mask_number(value)
            elif is_safe_record_reference(key, value):
                continue
            else:
                result[key] = project_read(value, doctype)
        return result
    return mask_display(data) if isinstance(data, str) else data


@frappe.whitelist()
def get(doctype, name=None, filters=None, parent=None):
    from privacy_shield.desk import get as original
    result = original(doctype, name, filters, parent)
    return project_read(result, doctype) if doctype in TARGETS and restricted() else result


@frappe.whitelist()
def get_list(doctype, fields=None, filters=None, group_by=None, order_by=None,
             limit_start=None, limit_page_length=20, parent=None, debug=False,
             as_dict=True, or_filters=None):
    from privacy_shield.listing import get_list as original
    result = original(doctype, fields, filters, group_by, order_by, limit_start,
                      limit_page_length, parent, debug, as_dict, or_filters)
    return project_read(result, doctype) if doctype in TARGETS and restricted() else result


@frappe.whitelist()
def get_value(doctype, fieldname, filters=None, as_dict=True, debug=False, parent=None):
    from privacy_shield.listing import get_value as original
    result = original(doctype, fieldname, filters, as_dict, debug, parent)
    return project_read(result, doctype) if doctype in TARGETS and restricted() else result
