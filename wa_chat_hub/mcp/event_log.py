from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe.utils import now_datetime
from frappe.utils.response import json_handler


MAX_JSON_CHARS = 20000
MAX_TRACEBACK_CHARS = 20000


def now_ms() -> float:
    return time.monotonic() * 1000


def elapsed_ms(started_ms: float | None) -> float | None:
    if not started_ms:
        return None
    return round(now_ms() - started_ms, 3)


def log_mcp_event(
    *,
    tool_name: str,
    status: str,
    execution_source: str = "AI",
    request_payload: Any = None,
    response_payload: Any = None,
    error: str | None = None,
    traceback_text: str | None = None,
    duration_ms: float | None = None,
    tool_meta: dict[str, Any] | None = None,
    tool_context: dict[str, Any] | None = None,
) -> str | None:
    """Best-effort MCP event log. Never raises back into MCP execution."""
    try:
        if not frappe.db.exists("DocType", "MCP Event"):
            return None

        tool_name = str(tool_name or "").strip()
        tool_meta = tool_meta or {}
        tool_context = tool_context or {}
        request_dict = request_payload if isinstance(request_payload, dict) else {}
        conversation = str(
            tool_context.get("conversation") or request_dict.get("conversation") or ""
        ).strip()
        channel_account = tool_context.get("channel_account") or _conversation_value(
            conversation, "channel_account"
        )
        patient = tool_context.get("patient") or request_dict.get("patient")
        crm_lead = tool_context.get("crm_lead")

        reference_doctype = None
        reference_name = None
        if patient:
            reference_doctype = "Patient"
            reference_name = patient
        elif crm_lead:
            reference_doctype = "CRM Lead"
            reference_name = crm_lead

        doc = frappe.get_doc(
            {
                "doctype": "MCP Event",
                "event_time": now_datetime(),
                "status": "Success" if status == "Success" else "Failed",
                "execution_source": execution_source or "AI",
                "tool": tool_name if frappe.db.exists("WA MCP Tool Endpoint", tool_name) else None,
                "tool_name": tool_name or "unknown",
                "access_mode": tool_meta.get("access_mode") or "Read",
                "http_method": tool_meta.get("method") or tool_meta.get("http_method"),
                "endpoint_url": tool_meta.get("url") or tool_meta.get("endpoint_url"),
                "duration_ms": duration_ms,
                "conversation": conversation or None,
                "channel_account": channel_account,
                "patient": patient if _exists("Patient", patient) else None,
                "crm_lead": crm_lead if _exists("CRM Lead", crm_lead) else None,
                "agent_profile": tool_context.get("agent_profile")
                if _exists("WA AI Agent Profile", tool_context.get("agent_profile"))
                else None,
                "reference_doctype": reference_doctype,
                "reference_name": reference_name,
                "request_json": _json_dump(request_payload),
                "response_json": _json_dump(response_payload),
                "error": str(error or "")[:1000],
                "traceback": str(traceback_text or "")[:MAX_TRACEBACK_CHARS],
            }
        )
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub MCP Event Log Failed")
        return None


def _json_dump(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = json.dumps(value, default=json_handler, indent=2)
    except Exception:
        text = json.dumps({"value": str(value)}, indent=2)
    if len(text) > MAX_JSON_CHARS:
        return text[:MAX_JSON_CHARS] + "\n...[truncated]"
    return text


def _conversation_value(conversation: str | None, fieldname: str):
    if not conversation or not frappe.db.exists("Chat Conversation", conversation):
        return None
    return frappe.db.get_value("Chat Conversation", conversation, fieldname)


def _exists(doctype: str, name: str | None) -> bool:
    return bool(name and frappe.db.exists("DocType", doctype) and frappe.db.exists(doctype, name))
