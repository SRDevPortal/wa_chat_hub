from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe.utils import now_datetime


HOT_TERMS = {
    "urgent",
    "emergency",
    "serious",
    "pain",
    "blood",
    "creatinine",
    "kidney",
    "dialysis",
    "report",
    "appointment",
    "consult",
    "call",
}
MEDIUM_TERMS = {"problem", "issue", "symptom", "medicine", "treatment", "doctor", "help", "test"}
DISEASE_HINTS = {
    "kidney": "Kidney Related Problem",
    "creatinine": "Kidney Related Problem",
    "dialysis": "Kidney Related Problem",
    "stone": "Kidney Stone",
    "urine": "Urinary Problem",
    "skin": "Skin Related Problem",
    "acne": "Skin Related Problem",
    "eczema": "Skin Related Problem",
}


def on_chat_message_after_insert(doc, method=None):
    conversation = getattr(doc, "conversation", None)
    if not conversation:
        return

    convo = frappe.get_doc("Chat Conversation", conversation)
    if convo.linked_reference_doctype != "CRM Lead" or not convo.linked_reference_name:
        return

    try:
        auto_update_lead_from_conversation(convo.linked_reference_name, conversation=conversation)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Lead AI Auto Update Failed")


def auto_update_lead_from_conversation(lead_name: str, conversation: str | None = None) -> dict[str, Any]:
    if not frappe.db.exists("CRM Lead", lead_name):
        return {"updated": False, "reason": "Lead not found"}

    lead = frappe.get_doc("CRM Lead", lead_name)
    context = _get_context_for_lead(lead)
    if context and not context.auto_update_lead_fields:
        return {"updated": False, "reason": "Context auto update disabled"}

    text = _build_lead_text(lead, conversation)
    scoring = _score_text(text)
    extraction = _extract_medical_details(text)
    applied = _apply_to_lead(lead, scoring, extraction)

    if applied:
        _create_insight(
            lead=lead,
            conversation=conversation,
            context=context,
            insight_type="Scoring",
            output={**scoring, **extraction},
            applied_fields=applied,
            input_snapshot=text[:5000],
        )

    return {"updated": bool(applied), "applied_fields": applied, "score": scoring}


def _get_context_for_lead(lead):
    pipeline = lead.get("sr_lead_pipeline")
    if not pipeline or not frappe.db.exists("DocType", "WA Channel Context"):
        return None

    name = frappe.db.get_value("WA Channel Context", {"pipeline": pipeline, "is_active": 1}, "name")
    return frappe.get_doc("WA Channel Context", name) if name else None


def _build_lead_text(lead, conversation: str | None = None) -> str:
    parts = []
    for fieldname in ("sr_lead_message", "sr_lead_notes", "sr_lead_disease", "wa_ai_extracted_summary"):
        if lead.get(fieldname):
            parts.append(str(lead.get(fieldname)))

    if conversation:
        rows = frappe.get_all(
            "Chat Message",
            filters={"conversation": conversation},
            fields=["body"],
            order_by="creation desc",
            limit_page_length=20,
        )
        parts.extend(row.body for row in rows if row.body)

    return "\n".join(parts)


def _score_text(text: str) -> dict[str, Any]:
    normalized = text.lower()
    hot_hits = sorted(term for term in HOT_TERMS if term in normalized)
    medium_hits = sorted(term for term in MEDIUM_TERMS if term in normalized)

    score = min(100, 35 + len(hot_hits) * 10 + len(medium_hits) * 5)
    if score >= 70:
        band = "Hot"
        next_action = "Call and schedule consultation"
    elif score >= 45:
        band = "Medium"
        next_action = "Follow up and collect missing details"
    else:
        band = "Low"
        next_action = "Send nurture message"

    reason_terms = hot_hits[:5] or medium_hits[:5]
    reason = "Matched signals: " + ", ".join(reason_terms) if reason_terms else "Limited urgency or medical intent signals found"
    return {
        "score": score,
        "score_band": band,
        "score_reason": reason,
        "next_action": next_action,
        "confidence": min(95, 45 + len(reason_terms) * 8),
    }


def _extract_medical_details(text: str) -> dict[str, Any]:
    normalized = text.lower()
    disease = None
    for keyword, value in DISEASE_HINTS.items():
        if keyword in normalized:
            disease = value
            break

    symptoms = _extract_sentence_matches(text, ["pain", "problem", "symptom", "swelling", "blood", "urine", "fever"])
    report_findings = _extract_sentence_matches(text, ["creatinine", "urea", "egfr", "report", "test", "scan", "ultrasound"])
    summary = _compact_summary(text)

    return {
        "disease": disease,
        "summary": summary,
        "symptoms": symptoms,
        "report_findings": report_findings,
        "medical_confidence": 80 if disease else 45,
    }


def _apply_to_lead(lead, scoring: dict[str, Any], extraction: dict[str, Any]) -> list[str]:
    meta = frappe.get_meta("CRM Lead")
    updates = {}

    field_map = {
        "wa_ai_score_band": scoring["score_band"],
        "wa_ai_score": scoring["score"],
        "wa_ai_confidence": scoring["confidence"],
        "wa_ai_score_reason": scoring["score_reason"],
        "wa_ai_next_action": scoring["next_action"],
        "wa_ai_last_scored_on": now_datetime(),
        "wa_ai_review_status": "Pending Review",
        "wa_ai_extracted_summary": extraction["summary"],
        "wa_ai_extracted_symptoms": extraction["symptoms"],
        "wa_ai_extracted_report_findings": extraction["report_findings"],
        "wa_ai_medical_confidence": extraction["medical_confidence"],
        "wa_ai_last_extracted_on": now_datetime(),
    }
    for fieldname, value in field_map.items():
        if meta.has_field(fieldname):
            updates[fieldname] = value

    if extraction.get("disease") and meta.has_field("sr_lead_disease") and not lead.get("sr_lead_disease"):
        updates["sr_lead_disease"] = extraction["disease"]

    if extraction.get("summary") and meta.has_field("sr_lead_notes"):
        existing_notes = lead.get("sr_lead_notes") or ""
        ai_line = f"AI Summary: {extraction['summary']}"
        if ai_line not in existing_notes:
            updates["sr_lead_notes"] = _append_note(existing_notes, ai_line)

    if not updates:
        return []

    frappe.db.set_value("CRM Lead", lead.name, updates, update_modified=True)
    return sorted(updates)


def _create_insight(lead, conversation, context, insight_type: str, output: dict[str, Any], applied_fields: list[str], input_snapshot: str):
    convo = frappe.get_doc("Chat Conversation", conversation) if conversation else None
    doc = frappe.get_doc(
        {
            "doctype": "WA Lead AI Insight",
            "lead": lead.name,
            "conversation": conversation,
            "channel_context": context.name if context else None,
            "channel_account": convo.channel_account if convo else None,
            "pipeline": lead.get("sr_lead_pipeline"),
            "insight_type": insight_type,
            "confidence": output.get("confidence") or output.get("medical_confidence"),
            "input_snapshot": input_snapshot,
            "output_json": json.dumps(output, default=str),
            "applied_fields": ", ".join(applied_fields),
        }
    )
    doc.insert(ignore_permissions=True)


def _extract_sentence_matches(text: str, keywords: list[str]) -> str:
    sentences = re.split(r"(?<=[.!?])\s+|\n+", text)
    matches = []
    for sentence in sentences:
        clean = sentence.strip()
        if clean and any(keyword in clean.lower() for keyword in keywords):
            matches.append(clean[:240])
        if len(matches) >= 3:
            break
    return "\n".join(matches)


def _compact_summary(text: str) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    return clean[:500]


def _append_note(existing: str, addition: str) -> str:
    existing = existing.strip()
    if not existing:
        return addition
    return f"{existing}\n\n{addition}"
