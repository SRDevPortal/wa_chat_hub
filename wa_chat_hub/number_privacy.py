"""Privacy at reviewed browser boundaries; never mutate stored/provider data."""
from copy import deepcopy
from functools import wraps
import re
import frappe


def enabled():
    return bool(frappe.conf.get("privacy_shield_desk_enabled", False)) and "privacy_shield" in frappe.get_installed_apps()


def restricted():
    if not enabled():
        return False
    from privacy_shield.policy import current_capabilities
    return not current_capabilities().view_full


RAW_KEYS = frozenset(("raw_payload", "raw_transport_payload", "payload", "provider_response",
                      "ai_workflow_state", "pending_patient_request", "pending_request_message"))
PHONE_KEYS = frozenset((
    "phone_number", "contact_phone_number", "channel_phone_number",
    "phoneNumber", "mobile_no", "mobile", "phone", "user_phone",
    "normalized_phone", "extracted_phone", "searched_phone",
    "custom_whatsapp_number", "aliased_phone", "sr_mobile_norm",
    "vobiz_mobile_last10", "vobiz_normalized_phone",
    "vobiz_phone_last10", "vobiz_whatsapp_last10",
))
TEXT_KEYS = frozenset(("body", "last_message_preview", "ai_summary", "display_name", "contact_display_name",
                       "error", "warning", "message", "content", "sender_name", "short_reason"))


def is_safe_record_reference(field, value):
    """Keep recognized CRM/Patient identifiers; never exempt phone-based IDs."""
    if not isinstance(value, str):
        return False
    patterns = {
        "linked_crm_lead": r"CRM-LEAD-[0-9]{4}-[0-9]{6}",
        "linked_lead": r"CRM-LEAD-[0-9]{4}-[0-9]{6}",
        "linked_patient": r"HLC-PAT-[0-9]{4}-[0-9]{5}",
        "linked_reference_name": r"(?:CRM-LEAD-[0-9]{4}-[0-9]{6}|HLC-PAT-[0-9]{4}-[0-9]{5})",
        "source_name": r"(?:CRM-LEAD-[0-9]{4}-[0-9]{6}|HLC-PAT-[0-9]{4}-[0-9]{5})",
    }
    return bool(field in patterns and re.fullmatch(patterns[field], value))


def project(payload):
    """Copy before filtering, including cache hits and shared ORM dictionaries."""
    from privacy_shield.masking import mask_number
    from privacy_shield.display_text import mask_display
    def clean(value, context=None):
        if isinstance(value, list):
            return [clean(item, context) for item in value]
        if not isinstance(value, dict):
            return deepcopy(value)
        result = {}
        for key, item in value.items():
            if key in RAW_KEYS:
                continue
            if key == "contact" and not isinstance(item, dict):
                continue  # Chat Contact names can be full phone numbers.
            if key == "name" and (context == "contact" or value.get("doctype") == "Chat Contact"):
                continue
            if key in PHONE_KEYS:
                result[key] = item if isinstance(item, str) and re.fullmatch(r"\*{1,14}[0-9]{0,4}|\[masked\]", item) else mask_number(item)
            elif key in TEXT_KEYS and isinstance(item, str):
                result[key] = mask_display(item)
            else:
                result[key] = clean(item, key)
        return result
    return clean(payload)


def browser_response(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        result = fn(*args, **kwargs)
        return project(result) if restricted() else result
    return wrapped


def message_event(conversation, message):
    if enabled():
        return {"conversation": conversation, "message_id": message.name, "refresh_required": True, "direction": message.direction, "sender_type": message.sender_type}
    return {"conversation": conversation, "message": message.as_dict(), "direction": message.direction}


TEMPLATE_FIELDS = frozenset(("contact.display_name", "contact.phone_number", "contact.linked_lead",
                            "contact.linked_patient", "conversation.department", "conversation.assigned_to"))


def resolve_template_values(values, conversation):
    """Called only after conversation authorization; accept fixed field references."""
    result = []
    contact = None
    for value in values:
        if isinstance(value, dict):
            field = value.get("field")
            if set(value) != {"field"} or field not in TEMPLATE_FIELDS:
                raise frappe.ValidationError("Unsupported template field")
            source, key = field.split(".")
            if source == "contact":
                if contact is None:
                    contact = frappe.get_doc("Chat Contact", conversation.contact)
                value = contact.get(key)
            else:
                value = conversation.get(key)
            value = str(value or "")
        elif not isinstance(value, str):
            raise frappe.ValidationError("Template values must be text or approved field references")
        result.append(value)
    return result
