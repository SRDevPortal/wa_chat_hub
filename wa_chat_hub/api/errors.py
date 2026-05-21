from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime


ERROR_CATEGORIES = {
    "webhook": {
        "label": "Webhook",
        "methods": ("Interakt Webhook Error", "Interakt Webhook Validation", "Interakt Inbound Missing Phone"),
        "suggestion": "Check Interakt webhook URL, channel account, phone extraction, and webhook secret.",
    },
    "lead": {
        "label": "Lead Creation",
        "methods": (
            "WA Chat Hub Inbound Lead Create Failed",
            "WA Chat Hub Inbound Lead Skipped",
            "WA Chat Hub Inbound Link Failed",
            "CRM Lead Meta Sync On Link Failed",
        ),
        "suggestion": "Check CRM Lead required fields, pipeline map, platform/source masters, and lead permissions.",
    },
    "outbound": {
        "label": "Outbound Send",
        "methods": ("Interakt Message API Failure", "WhatsApp Provider Send Message Failure", "WA Chat Hub Sending Failed"),
        "suggestion": "Check Interakt API key, WhatsApp window state, recipient phone, and provider response.",
    },
    "template_media": {
        "label": "Template / Media",
        "methods": (
            "Interakt Template Sending Failed",
            "Interakt Media Upload Failure",
            "Inbound Attachment Persistence Failed",
            "OCR Lead Summary Failed",
        ),
        "suggestion": "Check template approval/config, media type, file URL, and Interakt upload response.",
    },
    "ai": {
        "label": "AI Autopilot",
        "methods": (
            "WA AI Autopilot Enqueue Failed",
            "WA AI Autopilot Send Failed",
            "WA AI Fatal Error",
            "WA AI Provider API Failure",
            "WA AI Fallback Warning",
            "WA AI Autopilot Skipped (Messaging Window Closed)",
        ),
        "suggestion": "Check WA LLM Provider, API key, worker status, prompt config, and messaging window.",
    },
    "contact_sync": {
        "label": "Contact Sync",
        "methods": ("Interakt Contact Push Enqueue Failed", "Interakt Contact Sync Failed"),
        "suggestion": "Check Interakt account config and contact sync payload.",
    },
}


def _ensure_error_access() -> None:
    if "System Manager" in frappe.get_roles():
        return
    if frappe.db.exists("Role", "WA Chat Manager") and "WA Chat Manager" in frappe.get_roles():
        return
    frappe.throw(_("Not permitted to view WA Chat Hub error logs"), frappe.PermissionError)


@frappe.whitelist()
def get_error_logs(category: str | None = None, since: str = "24h", search: str | None = None, limit: int = 100):
    _ensure_error_access()

    rows = frappe.get_all(
        "Error Log",
        filters=_build_filters(since),
        fields=["name", "creation", "method", "error"],
        order_by="creation desc",
        limit_page_length=max(1, min(int(limit or 100), 500)),
    )

    events = []
    for row in rows:
        event = _format_error_event(row)
        if not event:
            continue
        if category and category not in {"all", event["category"]}:
            continue
        if search and not _matches_search(event, search):
            continue
        events.append(event)

    return {
        "success": True,
        "result": {
            "events": events,
            "summary": _build_summary(events),
            "categories": _category_options(),
        },
    }


@frappe.whitelist()
def get_error_detail(error_log: str):
    _ensure_error_access()
    if not error_log:
        frappe.throw(_("error_log is required"))

    row = frappe.get_doc("Error Log", error_log)
    event = _format_error_event(row.as_dict()) or {
        "name": row.name,
        "category": "other",
        "category_label": "Other",
        "method": row.method,
        "creation": row.creation,
        "short_reason": _short_reason(row.error),
        "phone": _extract_phone(row.error),
        "conversation": _extract_conversation(row.error),
        "suggestion": "Review the traceback and related records.",
    }
    event["error"] = row.error
    event["related"] = _related_records(event)
    return {"success": True, "result": event}


def _build_filters(since: str) -> Dict[str, Any]:
    since_dt = _since_datetime(since)
    return {"creation": [">=", since_dt]} if since_dt else {}


def _since_datetime(since: str):
    value = str(since or "24h").lower()
    if value == "today":
        return add_to_date(now_datetime(), days=0, as_string=False).replace(hour=0, minute=0, second=0, microsecond=0)
    if value == "7d":
        return now_datetime() - timedelta(days=7)
    if value == "24h":
        return now_datetime() - timedelta(hours=24)
    if value == "1h":
        return now_datetime() - timedelta(hours=1)
    return now_datetime() - timedelta(hours=24)


def _format_error_event(row) -> Optional[Dict[str, Any]]:
    method = row.get("method") or ""
    error = row.get("error") or ""
    category = _classify_error(method, error)
    if not category and "wa_chat_hub" not in error and "Interakt" not in error and "WA " not in method:
        return None

    category = category or "other"
    info = ERROR_CATEGORIES.get(category, {})
    return {
        "name": row.get("name"),
        "creation": row.get("creation"),
        "method": method,
        "category": category,
        "category_label": info.get("label") or "Other",
        "short_reason": _short_reason(error),
        "phone": _extract_phone(error),
        "conversation": _extract_conversation(error),
        "suggestion": info.get("suggestion") or "Review the traceback and related records.",
    }


def _classify_error(method: str, error: str) -> Optional[str]:
    text = f"{method}\n{error}"
    for category, info in ERROR_CATEGORIES.items():
        if any(marker and marker in text for marker in info["methods"]):
            return category
    return None


def _short_reason(error: str) -> str:
    lines = [line.strip() for line in str(error or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith(("frappe.", "pymysql.", "requests.", "Exception", "ValidationError")) or ":" in line:
            return line[:300]
    return (lines[-1] if lines else "No error details")[:300]


def _extract_phone(text: str) -> str:
    candidates = re.findall(r"(?<!\d)(?:\+?91)?[6-9]\d{9}(?!\d)", text or "")
    return candidates[0] if candidates else ""


def _extract_conversation(text: str) -> str:
    patterns = (
        r"conversation['\"]?\s*[:=]\s*['\"]?(\d+)",
        r"Conversation\s+(\d+)",
        r"conversation\s+(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text or "")
        if match:
            return match.group(1)
    return ""


def _matches_search(event: Dict[str, Any], search: str) -> bool:
    needle = str(search or "").strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        str(event.get(key) or "")
        for key in ("name", "method", "short_reason", "phone", "conversation", "category_label")
    ).lower()
    return needle in haystack


def _build_summary(events: list[Dict[str, Any]]) -> Dict[str, Any]:
    counts = {key: 0 for key in ERROR_CATEGORIES}
    counts["other"] = 0
    for event in events:
        counts[event["category"]] = counts.get(event["category"], 0) + 1
    return {"total": len(events), "by_category": counts}


def _category_options():
    return [{"value": key, "label": info["label"]} for key, info in ERROR_CATEGORIES.items()] + [
        {"value": "other", "label": "Other"}
    ]


def _related_records(event: Dict[str, Any]) -> Dict[str, Any]:
    related: Dict[str, Any] = {}
    conversation = event.get("conversation")
    if conversation and frappe.db.exists("Chat Conversation", conversation):
        related["conversation"] = conversation
        contact = frappe.db.get_value("Chat Conversation", conversation, "contact")
        if contact:
            related["contact"] = contact
        lead = frappe.db.get_value("Chat Conversation", conversation, "linked_crm_lead")
        if lead:
            related["crm_lead"] = lead

    phone = event.get("phone")
    if phone and not related.get("contact"):
        contact = frappe.db.get_value("Chat Contact", {"phone_number": ["like", f"%{phone[-10:]}"]}, "name")
        if contact:
            related["contact"] = contact

    return related
