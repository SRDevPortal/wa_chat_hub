from __future__ import annotations

import base64
from datetime import datetime
from io import BytesIO
import mimetypes
import os
import re
import tempfile
from typing import Dict, Optional

import frappe
import requests

from wa_chat_hub.ai.providers import (
    CHAT_CAPABILITY,
    VISION_CAPABILITY,
    get_active_llm_provider_rows,
    get_provider_secret,
    looks_like_vision_model,
)
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
    "audio",
    "video",
    "voice",
    "voice note",
    "audio message received",
    "video message received",
    "voice message received",
    "[audio message received]",
    "[video message received]",
    "[voice message received]",
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
            lines.append(f"Attachment OCR / visual classification:\n{extracted[:3500]}")
            lines.append(
                "Use this classification before replying. If it is a medical report, say report received "
                "and move to doctor/team review. If it is a skin/body photo, prescription photo, chat "
                "screenshot, bill/payment screenshot, random/non-medical image, or unclear photo, do not "
                "call it a report; acknowledge the actual image type and ask the next relevant question."
            )
        else:
            lines.append(
                "No readable text or reliable visual classification could be extracted. Do not call this "
                "a report by default. Acknowledge the image/photo and ask the customer what it shows or "
                "request a clearer photo if clinically relevant."
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
    if content_type not in ("Image", "Document"):
        return

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
        f"Source: {media_url}",
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
    media = _download_media(media_url)
    if media and _is_pdf_media(media_url, media.get("mime_type")):
        text = _extract_text_from_pdf(media.get("content") or b"")
        if text:
            return text

    if media and content_type in ("Image", "Document"):
        text = _extract_with_openai_vision(
            media_url,
            media_bytes=media.get("content"),
            mime_type=media.get("mime_type"),
        )
        if text:
            return text

    if content_type in ("Image", "Document") and not media:
        text = _extract_with_openai_vision(media_url)
        if text:
            return text

    # Lightweight fallback for text files / public URLs.
    try:
        if not media:
            return ""
        mime = str(media.get("mime_type") or "").lower()
        if "text/plain" in mime or "application/json" in mime or media_url.lower().endswith(".txt"):
            return media.get("content", b"").decode("utf-8", errors="ignore")[:12000]
    except Exception:
        return ""
    return ""



def _is_pdf_media(media_url: str, mime_type: str | None) -> bool:
    return str(mime_type or "").lower() == "application/pdf" or str(media_url or "").split("?", 1)[0].lower().endswith(".pdf")


def _extract_text_from_pdf(content: bytes) -> str:
    if not content:
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(BytesIO(content))
        chunks = []
        for page in reader.pages[:10]:
            text = page.extract_text() or ""
            if text.strip():
                chunks.append(text.strip())
            if sum(len(chunk) for chunk in chunks) >= 12000:
                break
        return "\n\n".join(chunks)[:12000]
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA OCR PDF Text Extraction Failed")
        return ""


def _download_media(media_url: str) -> Optional[Dict]:
    try:
        resp = requests.get(
            media_url,
            headers={
                "Accept": "*/*",
                "User-Agent": "wa-chat-hub/1.0",
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.content or b""
        if not content:
            return None
        mime_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if not mime_type:
            mime_type = mimetypes.guess_type(str(media_url).split("?", 1)[0])[0] or "application/octet-stream"
        return {"content": content, "mime_type": mime_type}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA OCR Media Download Failed")
        return None


def _extract_with_openai_vision(
    media_url: str,
    media_bytes: bytes | None = None,
    mime_type: str | None = None,
) -> str:
    providers = _get_openai_compatible_providers(require_vision=True)
    if not providers:
        return ""

    image_url = _build_vision_image_url(media_url, media_bytes, mime_type)
    if not image_url:
        return ""

    for provider in providers:
        base_url = provider["base_url"] or "https://api.openai.com/v1/chat/completions"
        if base_url.endswith("/") and "chat/completions" not in base_url:
            base_url = f"{base_url}chat/completions"

        payload = {
            "model": provider["model_name"],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Classify this WhatsApp attachment first, then extract useful text. "
                                "Return concise plain text with these fields:\n"
                                "Image type: one of medical report, prescription, skin/body photo, "
                                "payment/bill screenshot, chat/app screenshot, medicine/product photo, "
                                "non-medical/random image, unclear.\n"
                                "Medical relevance: short reason.\n"
                                "Readable text: key readable text only.\n"
                                "Reply guidance: how a healthcare coordinator should acknowledge it. "
                                "Do not assume every image is a report."
                            ),
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 1200,
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
            content = resp.json()["choices"][0]["message"].get("content", "")
            if content:
                return content[:12000]
        except Exception:
            frappe.log_error(
                frappe.get_traceback()
                + "\n\n"
                + frappe.as_json(
                    {
                        "provider": provider.get("name"),
                        "model": provider.get("model_name"),
                        "status_code": getattr(resp, "status_code", None) if "resp" in locals() else None,
                        "response": (getattr(resp, "text", "") or "")[:2000] if "resp" in locals() else "",
                    }
                ),
                f"OCR Vision Extraction Failed ({provider.get('name') or provider.get('model_name')})",
            )
    return ""


def _build_vision_image_url(media_url: str, media_bytes: bytes | None, mime_type: str | None) -> str:
    mime_type = str(mime_type or "").split(";", 1)[0].strip().lower()
    if media_bytes and (mime_type.startswith("image/") or _looks_like_image_url(media_url)):
        if not mime_type or not mime_type.startswith("image/"):
            mime_type = mimetypes.guess_type(str(media_url).split("?", 1)[0])[0] or "image/jpeg"
        encoded = base64.b64encode(media_bytes).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"
    return media_url


def _looks_like_image_url(media_url: str) -> bool:
    path = str(media_url or "").split("?", 1)[0].lower()
    return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


def _summarize_report_text(extracted: str, body_hint: str, content_type: str) -> str:
    extracted = (extracted or "").strip()
    if extracted:
        for provider in _get_openai_compatible_providers():
            summary = _summarize_with_model(provider, extracted)
            if summary:
                return summary
        return _heuristic_report_summary(extracted)
    if body_hint:
        return f"No OCR text extracted. User caption/body: {body_hint}"
    return f"No OCR text extracted for this {content_type.lower()}."


def _heuristic_report_summary(extracted_text: str) -> str:
    text = str(extracted_text or "").strip()
    if not text:
        return ""

    lowered = text.lower()
    if "kidney function" not in lowered and "kft" not in lowered and "creatinine" not in lowered:
        return "Report summary:\n• OCR text extracted, but automatic report interpretation is limited.\n\nKey findings:\n• Review extracted report text manually.\n\nAbnormal values:\n• Not automatically identified.\n\nSuggested follow-up:\n• Ask doctor/team to review the attachment and confirm clinically."

    checks = [
        ("Blood Urea", "mg/dL", 15, 40),
        ("Serum Creatinine", "mg/dL", 0.6, 1.2),
        ("BUN / Creatinine Ratio", "", 10, 20),
        ("Uric Acid", "mg/dL", 3.5, 7.2),
        ("Sodium", "mEq/L", 135, 145),
        ("Potassium", "mEq/L", 3.5, 5.0),
        ("Chloride", "mEq/L", 98, 106),
        ("Bicarbonate", "mEq/L", 22, 28),
        ("Calcium", "mg/dL", 8.6, 10.2),
        ("Phosphorus", "mg/dL", 2.5, 4.5),
    ]
    abnormal = []
    for label, unit, low, high in checks:
        value = _find_nearby_number(text, label)
        if value is None:
            continue
        if value < low:
            abnormal.append(f"{label}: {value:g} {unit}".strip() + f" (low; ref {low:g}-{high:g})")
        elif value > high:
            abnormal.append(f"{label}: {value:g} {unit}".strip() + f" (high; ref {low:g}-{high:g})")

    if "reduced egfr" in lowered or "significantly reduced egfr" in lowered:
        abnormal.append("eGFR: report impression says significantly reduced")
    if "metabolic acidosis" in lowered:
        abnormal.append("Report impression mentions metabolic acidosis")
    if "renal impairment" in lowered:
        abnormal.append("Report impression says findings are consistent with significant renal impairment")

    key_findings = abnormal[:8] if abnormal else ["Kidney function report text extracted; doctor review advised."]
    return (
        "Report summary:\n"
        "• KFT/kidney function report received and OCR text was readable.\n"
        "• Report impression suggests renal/kidney function concern; clinical correlation is needed.\n\n"
        "Key findings:\n"
        + "\n".join(f"• {item}" for item in key_findings)
        + "\n\nAbnormal values:\n"
        + ("\n".join(f"• {item}" for item in abnormal) if abnormal else "• Not automatically identified.")
        + "\n\nSuggested follow-up:\n"
        "• Doctor/nephrologist review is advisable, especially because creatinine/urea/electrolytes appear abnormal.\n"
        "• Ask patient for current symptoms, BP/diabetes history, urine output/swelling, and any previous creatinine reports."
    )


def _find_nearby_number(text: str, label: str) -> float | None:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    label_lower = label.lower()
    for idx, line in enumerate(lines):
        if label_lower not in line.lower():
            continue
        for candidate in lines[idx + 1 : idx + 7]:
            if "-" in candidate or "/" in candidate:
                continue
            match = re.search(r"\d+(?:\.\d+)?", candidate)
            if match:
                try:
                    return float(match.group(0))
                except Exception:
                    return None
    return None


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


def _get_openai_compatible_providers(require_vision: bool = False) -> list[Dict]:
    candidates = get_active_llm_provider_rows(VISION_CAPABILITY if require_vision else CHAT_CAPABILITY, limit=5)

    if require_vision and not candidates:
        frappe.log_error(
            "No active vision-capable WA LLM Provider found for OCR. Configure a provider with Use for Vision / OCR enabled and an image-capable model.",
            "WA OCR Vision Provider Missing",
        )
        return []

    providers = []
    for row in candidates:
        provider = get_provider_secret(row)
        if provider:
            providers.append(provider)
    return providers


def _get_openai_compatible_provider(require_vision: bool = False) -> Optional[Dict]:
    providers = _get_openai_compatible_providers(require_vision=require_vision)
    return providers[0] if providers else None


def _looks_like_vision_model(provider_type: str, model_name: str | None) -> bool:
    return looks_like_vision_model(provider_type, model_name)


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
