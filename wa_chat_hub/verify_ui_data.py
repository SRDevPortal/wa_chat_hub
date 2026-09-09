import json
import frappe
from wa_chat_hub.api import chat

def run():
    out = {}
    rows = chat.get_conversations(limit=5) or []
    out["conversations_sample"] = []
    seq = rows[:3] if isinstance(rows, list) else list(rows)[:3]
    for r in seq:
        if isinstance(r, dict):
            out["conversations_sample"].append({k: r.get(k) for k in ["name","contact_display_name","lead_score","lead_lan","lead_temperature","last_message_preview"]})
        else:
            out["conversations_sample"].append({"raw": str(r)})
    conv_id = 71
    if not frappe.db.exists("Chat Conversation", str(conv_id)):
        conv_id = frappe.db.get_value("Chat Conversation", {"status": "Open"}, "name", order_by="modified desc")
    out["conversation_id"] = conv_id
    msgs = chat.get_messages(conversation=conv_id) or []
    inbound_media = []
    for m in reversed(msgs if isinstance(msgs, list) else []):
        if not isinstance(m, dict):
            continue
        direction = (m.get("direction") or m.get("message_direction") or "").lower()
        is_in = direction in ("inbound", "incoming", "received") or m.get("is_incoming") in (1, True)
        if not is_in:
            continue
        ct = m.get("content_type") or ""
        media = m.get("media_url") or m.get("attachment_file")
        if ct in ("image","video","audio","document","file","sticker") or media:
            inbound_media.append({k: m.get(k) for k in ["content_type","media_url","attachment_file","name"]})
        if len(inbound_media) >= 3:
            break
    out["inbound_media_last3"] = inbound_media[:3]
    lead = "CRM-LEAD-2026-00026"
    doc = frappe.get_doc("Lead", lead)
    out["crm_lead"] = {"lead_score": doc.get("lead_score"), "lead_lan": doc.get("lead_lan"), "lead_temperature": doc.get("lead_temperature")}
    notes_text = (doc.get("comments") or "")
    for row in frappe.get_all("CRM Note", filters={"parent": lead, "parenttype": "Lead"}, fields=["content","note"]):
        notes_text += " " + str(row.get("content") or row.get("note") or "")
    out["crm_lead"]["auto_ocr_in_notes_comments"] = "Auto OCR Summary" in notes_text
    try:
        out["resolve_chat"] = chat.resolve_chat_for_reference(reference_doctype="Lead", reference_name=lead)
    except Exception as e:
        out["resolve_chat"] = {"error": str(e)}
    print(json.dumps(out, default=str, indent=2))