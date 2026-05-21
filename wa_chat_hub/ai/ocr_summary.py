from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

import frappe
import requests

from wa_chat_hub.prompts import get_conversation_crm_lead

# Max length for sr_lead_notes (Small Text); keep headroom for separators.
SR_LEAD_NOTES_MAX_LEN = 6000

OCR_NOTE_SEPARATOR = "─" * 36

GENERIC_MEDIA_BODIES = {
    "",
    "none",
    "null",
    "undefined",
    "photo",
    "image",
    "document",
    "image message received",
    "document message received",
    "[image message received]",
    "[document message received]",
}


def build_media_context_for_chat(
    media_url: str,
    content_type: str,
    body_hint: str = "",
) -> str:
    """Plain-text context about an inbound attachment for AI autopilot (not CRM notes)."""
    content_type = str(content_type or "Document").title()
    caption = _clean_media_caption(body_hint)
    lines = [f"Customer sent a {content_type} attachment on WhatsApp."]
    if caption:
        lines.append(f"Caption: {caption}")

    if content_type in ("Image", "Document"):
        extracted = _extract_text_from_media(media_url, content_type)
        if not extracted and content_type == "Document":
            extracted = _extract_with_openai_vision(media_url)
        if extracted:
            lines.append(f"Extracted text from attachment:\n{extracted[:3500]}")
        else:
            lines.append(
                "No readable text could be extracted. Acknowledge receipt and ask for a clearer "
                "photo or typed details if clinically relevant."
            )
    elif content_type in ("Video", "Audio"):
        lines.append(
            "Respond naturally to the media message. Ask for a photo or typed report details "
            "if they are sharing clinical information."
        )
    else:
        lines.append(f"Attachment URL: {media_url[:200]}")

    return "\n".join(lines)


def _clean_media_caption(body_hint: str) -> str:
    text = str(body_hint or "").strip()
    if text.lower() in GENERIC_MEDIA_BODIES:
        return ""
    return text


def process_attachment_for_lead_summary(
    conversation: str,
    message_name: str,
    payload: Dict,
) -> None:
    convo = frappe.get_doc("Chat Conversation", conversation)
    crm_lead = get_conversation_crm_lead(convo)
    if not crm_lead:
        return

    media_url = payload.get("media_url")
    if not media_url:
        media_url = frappe.db.get_value("Chat Message", message_name, "media_url")
    if not media_url:
        return

    content_type = str(payload.get("content_type") or "Document").title()
    body_hint = str(payload.get("body") or "").strip()
    extracted = _extract_text_from_media(media_url, content_type)
    summary = _summarize_report_text(extracted, body_hint, content_type)
    note_block = _build_sr_lead_notes_block(
        content_type=content_type,
        message_name=message_name,
        media_url=media_url,
        body_hint=body_hint,
        summary=summary,
    )
    _append_to_lead_notes("CRM Lead", crm_lead, note_block)


def _build_sr_lead_notes_block(
    content_type: str,
    message_name: str,
    media_url: str,
    body_hint: str,
    summary: str,
) -> str:
    """Structured OCR block for CRM Lead sr_lead_notes (plain-text, sectioned)."""
    stamp = datetime.now().strftime("%d-%b-%Y %H:%M")
    caption = body_hint.strip() if body_hint and body_hint.lower() not in {
        "none", "null", "undefined", "[image message received]", "[document message received]",
    } else ""
    lines = [
        f"[WA-OCR | {stamp} | {content_type}]",
        f"Ref: Chat Message {message_name}",
    ]
    if caption:
        lines.append(f"Caption: {caption}")
    lines.extend([
        OCR_NOTE_SEPARATOR,
        _normalize_summary_sections(summary.strip()),
        OCR_NOTE_SEPARATOR,
        f"Source: {media_url[:200]}",
    ])
    return "\n".join(lines)


def _normalize_summary_sections(summary: str) -> str:
    """Ensure summary uses expected section headers for sr_lead_notes."""
    if not summary:
        return (
            "Report summary:\n"
            "• No readable text extracted from attachment.\n\n"
            "Suggested follow-up:\n"
            "• Ask patient to resend a clearer photo or PDF of the report."
        )

    required_headers = (
        "Report summary:",
        "Key findings:",
        "Abnormal values:",
        "Suggested follow-up:",
    )
    lowered = summary.lower()
    if any(header.lower() in lowered for header in required_headers):
        return summary

    return (
        "Report summary:\n"
        f"• {summary.replace(chr(10), chr(10) + '• ')}\n\n"
        "Key findings:\n"
        "• See report summary above.\n\n"
        "Abnormal values:\n"
        "• Not explicitly flagged.\n\n"
        "Suggested follow-up:\n"
        "• Review attachment and confirm clinically."
    )


def _resolve_notes_fieldname(doctype: str) -> Optional[str]:
    meta = frappe.get_meta(doctype)
    if meta.has_field("sr_lead_notes"):
        return "sr_lead_notes"
    if meta.has_field("notes"):
        return "notes"
    return None


def _append_to_lead_notes(doctype: str, name: str, note_block: str) -> None:
    lead_doc = frappe.get_doc(doctype, name)
    notes_field = _resolve_notes_fieldname(doctype)
    if notes_field:
        existing = str(getattr(lead_doc, notes_field, "") or "").strip()
        merged = f"{existing}\n\n{note_block}".strip() if existing else note_block
        if len(merged) > SR_LEAD_NOTES_MAX_LEN:
            merged = _trim_notes_to_limit(existing, note_block, SR_LEAD_NOTES_MAX_LEN)
        setattr(lead_doc, notes_field, merged)
        lead_doc.save(ignore_permissions=True)
    lead_doc.add_comment("Comment", note_block)


def _trim_notes_to_limit(existing: str, new_block: str, max_len: int) -> str:
    """Keep newest OCR block; drop oldest content if over Small Text limit."""
    if len(new_block) >= max_len:
        return new_block[: max_len - 20] + "\n… [truncated]"
    budget = max_len - len(new_block) - 2
    if not existing:
        return new_block
    if len(existing) <= budget:
        return f"{existing}\n\n{new_block}".strip()
    trimmed_existing = existing[-budget:]
    if "\n\n" in trimmed_existing:
        trimmed_existing = trimmed_existing.split("\n\n", 1)[-1]
    return f"… [older notes truncated]\n\n{trimmed_existing}\n\n{new_block}".strip()


def _extract_text_from_media(media_url: str, content_type: str) -> str:
    if content_type in ("Image", "Document"):
        text = _extract_with_openai_vision(media_url)
        if text:
            return text

    # Lightweight fallback for text files / public URLs
    try:
        resp = requests.get(media_url, timeout=20)
        if not resp.ok:
            return ""
        mime = str(resp.headers.get("Content-Type") or "").lower()
        if "text/plain" in mime or "application/json" in mime or media_url.lower().endswith(".txt"):
            return resp.text[:12000]
    except Exception:
        return ""
    return ""


def _extract_with_openai_vision(media_url: str) -> str:
    provider = _get_openai_compatible_provider()
    if not provider:
        return ""

    base_url = provider["base_url"] or "https://api.openai.com/v1/chat/completions"
    if base_url.endswith("/") and "chat/completions" not in base_url:
        base_url = f"{base_url}chat/completions"

    payload = {
        "model": provider["model_name"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Extract all readable medical/report text from this image."},
                    {"type": "image_url", "image_url": {"url": media_url}},
                ],
            }
        ],
        "temperature": 0,
    }
    try:
        resp = requests.post(
            base_url,
            headers={
                "Authorization": f"Bearer {provider['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"].get("content", "")[:12000]
    except Exception:
        frappe.log_error(frappe.get_traceback(), "OCR Vision Extraction Failed")
        return ""


def _summarize_report_text(extracted: str, body_hint: str, content_type: str) -> str:
    extracted = (extracted or "").strip()
    if extracted:
        provider = _get_openai_compatible_provider()
        if provider:
            summary = _summarize_with_model(provider, extracted)
            if summary:
                return summary
        # Heuristic fallback summary
        return (
            "Summary (fallback):\n"
            + extracted[:1200]
        )
    if body_hint:
        return f"No OCR text extracted. User caption/body: {body_hint}"
    return f"No OCR text extracted for this {content_type.lower()}."


def _summarize_with_model(provider: Dict, extracted_text: str) -> str:
    base_url = provider["base_url"] or "https://api.openai.com/v1/chat/completions"
    if base_url.endswith("/") and "chat/completions" not in base_url:
        base_url = f"{base_url}chat/completions"
    prompt = (
        "Summarize this medical report for CRM lead notes. "
        "Return ONLY plain text using exactly these section headers and bullet lines:\n"
        "Report summary:\n"
        "• <1-3 short bullets>\n\n"
        "Key findings:\n"
        "• <bullets>\n\n"
        "Abnormal values:\n"
        "• <bullets or 'None noted'>\n\n"
        "Suggested follow-up:\n"
        "• <bullets as questions, no diagnosis or prescriptions>\n\n"
        f"Report text:\n{extracted_text[:10000]}"
    )
    try:
        resp = requests.post(
            base_url,
            headers={
                "Authorization": f"Bearer {provider['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": provider["model_name"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"].get("content", "")[:4000]
    except Exception:
        frappe.log_error(frappe.get_traceback(), "OCR Summary Generation Failed")
        return ""


def _get_openai_compatible_provider() -> Optional[Dict]:
    rows = frappe.get_all(
        "WA LLM Provider",
        filters={"is_active": 1},
        fields=["name", "provider_type", "model_name", "base_url"],
        order_by="priority asc",
        limit=5,
    )
    for row in rows:
        if row.provider_type not in {"OpenAI", "Gemini", "Custom"}:
            continue
        doc = frappe.get_doc("WA LLM Provider", row.name)
        api_key = doc.get_password("api_key")
        if not api_key:
            continue
        return {
            "name": row.name,
            "provider_type": row.provider_type,
            "model_name": row.model_name,
            "base_url": row.base_url,
            "api_key": api_key,
        }
    return None


def build_attachment_filename(payload: Dict, media_url: str, fallback_prefix: str = "wa-attachment") -> str:
    explicit = str(payload.get("file_name") or "").strip()
    if explicit:
        return explicit[:140]
    url_part = str(media_url or "").split("?")[0].rstrip("/").split("/")[-1]
    if url_part:
        return url_part[:140]
    content_type = str(payload.get("content_type") or "").lower()
    ext_map = {"image": ".jpg", "document": ".pdf", "video": ".mp4", "audio": ".mp3"}
    ext = ext_map.get(content_type, "")
    return f"{fallback_prefix}-{frappe.generate_hash(length=8)}{ext}"

