from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cint

from wa_chat_hub.prompts import get_conversation_crm_lead
from wa_chat_hub.security import safe_ai_get_all, safe_ai_get_doc


CRM_LEAD_FIELDS = (
    "name",
    "lead_name",
    "first_name",
    "middle_name",
    "last_name",
    "email",
    "mobile_no",
    "phone",
    "gender",
    "sr_lead_country",
    "sr_lead_message",
    "sr_lead_notes",
    "sr_lead_disease",
    "sr_lead_pipeline",
    "lead_owner",
    "source",
    "sr_lead_platform",
    "status",
    "sr_lead_disposition",
    "lead_score",
    "lead_lan",
    "lead_temperature",
    "organization",
    "territory",
    "industry",
    "job_title",
    "team",
    "converted",
    "communication_status",
    "vobiz_last_call_status",
    "vobiz_last_call_time",
    "vobiz_next_follow_up",
    "vobiz_total_call_attempts",
    "vobiz_connected_call_count",
    "vobiz_caller_classification",
    "vobiz_ai_intent",
    "vobiz_ai_summary",
    "vobiz_ai_concerns",
)

CRM_NOTE_FIELDS = (
    "name",
    "note",
    "added_by",
    "added_on",
    "creation",
)


def get_linked_crm_lead_profile(
    *, conversation: str, include_notes: int = 1, limit: int = 5
) -> dict[str, Any]:
    """Return allowlisted CRM Lead details only for the lead linked to this chat."""
    conversation = str(conversation or "").strip()
    if not conversation:
        frappe.throw(_("Conversation context is required."), frappe.PermissionError)

    lead_name = get_conversation_crm_lead(conversation)
    if not lead_name:
        frappe.throw(_("No CRM Lead is linked to this conversation."), frappe.PermissionError)

    lead = safe_ai_get_doc("CRM Lead", lead_name)
    result: dict[str, Any] = {
        "conversation": conversation,
        "crm_lead": _allowlisted_doc(lead, CRM_LEAD_FIELDS),
    }

    if cint(include_notes) and frappe.db.exists("DocType", "CRM Note"):
        note_fields = _existing_fields("CRM Note", CRM_NOTE_FIELDS)
        notes = safe_ai_get_all(
            "CRM Note",
            filters={"parent": lead_name, "parenttype": "CRM Lead"},
            fields=note_fields,
            order_by="idx desc, modified desc",
            limit_page_length=_safe_limit(limit),
        )
        result["notes"] = [dict(row) for row in notes]

    return result


def _allowlisted_doc(doc, fields: tuple[str, ...]) -> dict[str, Any]:
    return {
        fieldname: doc.get(fieldname)
        for fieldname in fields
        if fieldname == "name" or doc.meta.has_field(fieldname)
    }


def _existing_fields(doctype: str, fields: tuple[str, ...]) -> list[str]:
    meta = frappe.get_meta(doctype)
    return [
        fieldname
        for fieldname in fields
        if fieldname == "name" or meta.has_field(fieldname)
    ]


def _safe_limit(value: int) -> int:
    return max(1, min(10, cint(value or 5)))
