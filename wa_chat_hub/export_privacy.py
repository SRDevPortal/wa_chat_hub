"""Scoped chat downloads: native authorization/filtering, masked output copies."""
import csv
import io

import frappe
from wa_chat_hub.number_privacy import restricted, is_safe_record_reference

FIELDS = {
    "Chat Contact": {"name", "display_name", "phone_number", "source_doctype", "source_name", "linked_patient", "linked_lead", "owner", "creation", "modified"},
    "Chat Conversation": {"name", "contact", "status", "priority", "channel_account", "assigned_to", "department", "linked_crm_lead", "linked_reference_doctype", "linked_reference_name", "last_message_preview", "last_message_time", "unread_count", "lead_score", "lead_lan", "lead_temperature", "owner", "creation", "modified"},
}


def validate_fields(doctype, fields):
    if isinstance(fields, str):
        fields = frappe.parse_json(fields)
    if not isinstance(fields, (list, tuple)) or not fields:
        raise frappe.PermissionError("Select specific chat columns for a masked export.")
    for field in fields:
        if not isinstance(field, str):
            raise frappe.PermissionError("Select only reviewed chat columns.")
        # Standard report builder qualifies fields with the source table name.
        plain = field.replace("`", "")
        prefix = "tab" + doctype + "."
        if plain.startswith(prefix):
            plain = plain[len(prefix):]
        if plain not in FIELDS[doctype]:
            raise frappe.PermissionError("This chat column is not available in masked exports.")


def mask_cell(value):
    from privacy_shield.display_text import mask_display
    from privacy_shield.outputs import safe_cell
    if isinstance(value, str):
        # Export layouts differ: preserve only an entire recognized record ID.
        # Phone-based Chat Contact IDs and embedded free text remain masked.
        if is_safe_record_reference("linked_reference_name", value):
            return safe_cell(value)
        return safe_cell(mask_display(value))
    if isinstance(value, int) and not isinstance(value, bool) and 10 <= len(str(abs(value))) <= 15:
        return mask_display(str(value))
    return value


def mask_download(file_type):
    response = frappe.response
    if file_type == "CSV":
        key = "filecontent" if "filecontent" in response else "result"
        content = response[key]
        text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
        output = io.StringIO(newline="")
        csv.writer(output).writerows([[mask_cell(cell) for cell in row] for row in csv.reader(io.StringIO(text))])
        response[key] = output.getvalue().encode("utf-8-sig") if isinstance(content, bytes) else output.getvalue()
    else:
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(response["filecontent"]))
        try:
            for sheet in workbook:
                for row in sheet:
                    for cell in row:
                        if cell.value is not None:
                            cell.value = mask_cell(cell.value)
            output = io.BytesIO()
            workbook.save(output)
            response["filecontent"] = output.getvalue()
        finally:
            workbook.close()
    if response.get("filename"):
        response["filename"] = mask_cell(response["filename"])


def protected_export(original, file_type, *args, **kwargs):
    if file_type not in ("CSV", "Excel"):
        raise frappe.ValidationError("Use CSV or Excel for masked chat exports.")
    result = original(*args, **kwargs)
    try:
        mask_download(file_type)
    except Exception:
        # Do not leave a raw download queued if formatting fails.
        for key in ("filecontent", "result", "type", "filename"):
            frappe.response.pop(key, None)
        raise frappe.ValidationError("Unable to prepare the masked chat export.") from None
    return result


@frappe.whitelist()
def export_data(doctype=None, parent_doctype=None, all_doctypes=True, with_data=False,
                select_columns=None, file_type="CSV", template=False, filters=None,
                export_without_column_meta=False):
    from sriaas_clinic.api.crm_lead.privacy_outputs import export_data as original
    target = doctype[0] if isinstance(doctype, list) and doctype else doctype
    target = target or parent_doctype
    args = (doctype, parent_doctype, all_doctypes, with_data, select_columns, file_type,
            template, filters, export_without_column_meta)
    if target not in FIELDS or not restricted():
        return original(*args)
    if parent_doctype not in (None, "", target) or str(template).lower() not in ("false", "0", "none", ""):
        raise frappe.PermissionError("Import templates and child exports are not available in masked chat exports.")
    columns = frappe.parse_json(select_columns) if isinstance(select_columns, str) else select_columns
    if not isinstance(columns, dict) or set(columns) != {target}:
        raise frappe.PermissionError("Select only reviewed columns from this chat document.")
    validate_fields(target, columns[target])
    return protected_export(original, file_type, *args)


@frappe.whitelist()
def export_query():
    from sriaas_clinic.api.crm_lead.privacy_outputs import export_query as original
    target = frappe.form_dict.get("doctype")
    if target not in FIELDS or not restricted():
        return original()
    validate_fields(target, frappe.form_dict.get("fields"))
    # A custom delimiter cannot be safely re-read by the standard CSV parser.
    if frappe.form_dict.get("csv_delimiter") not in (None, "", ","):
        raise frappe.ValidationError("Use the standard CSV separator for masked chat exports.")
    return protected_export(original, frappe.form_dict.get("file_format_type", "CSV"))
