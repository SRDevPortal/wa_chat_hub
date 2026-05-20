from __future__ import annotations

from pathlib import Path

import frappe


MODULE = "wa_chat_hub"


def after_migrate() -> None:
    """Keep WA Chat Hub standard doctypes and workspace synced after migrate."""
    sync_standard_doctypes()
    ensure_crm_lead_ai_fields()
    cleanup_removed_doctypes()
    try:
        from wa_chat_hub.setup_workspace import run as setup_workspace

        setup_workspace()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Workspace Sync Failed")


def sync_standard_doctypes() -> None:
    doctype_dir = Path(__file__).parent / MODULE / "doctype"
    if not doctype_dir.exists():
        return

    for json_file in sorted(doctype_dir.glob("*/*.json")):
        doctype_name = json_file.parent.name
        try:
            frappe.reload_doc(MODULE, "doctype", doctype_name, force=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"WA Chat Hub DocType Sync Failed: {doctype_name}",
            )


def ensure_crm_lead_ai_fields() -> None:
    if not frappe.db.exists("DocType", "CRM Lead"):
        return

    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(
        {
            "CRM Lead": [
                {
                    "fieldname": "wa_ai_section",
                    "label": "WA AI",
                    "fieldtype": "Section Break",
                    "insert_after": "sr_lead_disease",
                },
                {
                    "fieldname": "wa_ai_score_band",
                    "label": "AI Score Band",
                    "fieldtype": "Select",
                    "options": "Hot\nMedium\nLow",
                    "insert_after": "wa_ai_section",
                    "read_only": 1,
                    "in_list_view": 1,
                },
                {
                    "fieldname": "wa_ai_score",
                    "label": "AI Score",
                    "fieldtype": "Int",
                    "insert_after": "wa_ai_score_band",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_confidence",
                    "label": "AI Confidence",
                    "fieldtype": "Percent",
                    "insert_after": "wa_ai_score",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_column_break",
                    "fieldtype": "Column Break",
                    "insert_after": "wa_ai_confidence",
                },
                {
                    "fieldname": "wa_ai_review_status",
                    "label": "AI Review Status",
                    "fieldtype": "Select",
                    "options": "Pending Review\nReviewed\nCorrected",
                    "default": "Pending Review",
                    "insert_after": "wa_ai_column_break",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_last_scored_on",
                    "label": "AI Last Scored On",
                    "fieldtype": "Datetime",
                    "insert_after": "wa_ai_review_status",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_score_reason",
                    "label": "AI Score Reason",
                    "fieldtype": "Small Text",
                    "insert_after": "wa_ai_last_scored_on",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_next_action",
                    "label": "AI Next Action",
                    "fieldtype": "Small Text",
                    "insert_after": "wa_ai_score_reason",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_extracted_summary",
                    "label": "AI Extracted Summary",
                    "fieldtype": "Small Text",
                    "insert_after": "wa_ai_next_action",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_extracted_symptoms",
                    "label": "AI Extracted Symptoms",
                    "fieldtype": "Small Text",
                    "insert_after": "wa_ai_extracted_summary",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_extracted_report_findings",
                    "label": "AI Extracted Report Findings",
                    "fieldtype": "Small Text",
                    "insert_after": "wa_ai_extracted_symptoms",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_medical_confidence",
                    "label": "AI Medical Confidence",
                    "fieldtype": "Percent",
                    "insert_after": "wa_ai_extracted_report_findings",
                    "read_only": 1,
                },
                {
                    "fieldname": "wa_ai_last_extracted_on",
                    "label": "AI Last Extracted On",
                    "fieldtype": "Datetime",
                    "insert_after": "wa_ai_medical_confidence",
                    "read_only": 1,
                },
            ]
        },
        ignore_validate=True,
    )


def cleanup_removed_doctypes() -> None:
    removed = [
        "Chat Pipeline Channel Mapping",
        "WA Knowledge Base",
        "Chat Channel Session",
        "Chat Queue Event",
        "Chat Action Log",
        "WA MCP Server",
        "WA MCP Tool Endpoint",
        "WA AI Tool Permission",
    ]

    for doctype in removed:
        if not frappe.db.exists("DocType", doctype):
            continue
        if frappe.db.count(doctype):
            frappe.log_error(
                f"Skipped deleting {doctype} because it has records.",
                "WA Chat Hub Cleanup Skipped",
            )
            continue
        try:
            frappe.delete_doc("DocType", doctype, force=True, ignore_permissions=True)
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"WA Chat Hub DocType Cleanup Failed: {doctype}")
