from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
import requests
from frappe import _

from wa_chat_hub.mcp.event_log import elapsed_ms, log_mcp_event, now_ms
from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_value,
)


def build_mcp_runtime_context(department: Optional[str] = None) -> Dict[str, Any]:
    """Return MCP servers/tools available to the desk runtime."""
    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    allow_mcp = bool(getattr(settings, "allow_mcp_access", 0))

    servers = []
    if safe_ai_exists("DocType", "WA MCP Server"):
        filters: Dict[str, Any] = {"is_active": 1}
        if department:
            filters["department"] = ["in", ["", department]]
        servers = safe_ai_get_all(
            "WA MCP Server",
            filters=filters,
            fields=[
                "name",
                "server_label",
                "server_url",
                "transport_type",
                "department",
                "is_active",
            ],
            order_by="server_label asc",
        )

    tools = []
    if allow_mcp and safe_ai_exists("DocType", "WA MCP Tool Endpoint"):
        tools = safe_ai_get_all(
            "WA MCP Tool Endpoint",
            filters={"is_active": 1},
            fields=[
                "name",
                "tool_name",
                "description",
                "endpoint_url",
                "http_method",
                "access_mode",
                "server",
                "is_active",
            ],
            order_by="tool_name asc",
        )

    return {
        "allow_mcp_access": allow_mcp,
        "department": department,
        "servers": servers,
        "tools": tools,
    }


def invoke_mcp_tool(
    server_name: Optional[str] = None,
    tool_name: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute a configured MCP tool endpoint."""
    started_ms = now_ms()
    payload = payload or {}
    if not tool_name:
        frappe.throw(_("tool_name is required"))

    if not safe_ai_exists("DocType", "WA MCP Tool Endpoint"):
        frappe.throw(_("WA MCP Tool Endpoint is not installed"))

    row = safe_ai_get_value(
        "WA MCP Tool Endpoint",
        {"tool_name": tool_name, "is_active": 1},
        ["name", "endpoint_url", "http_method", "access_mode", "server"],
        as_dict=True,
    )
    if not row:
        log_mcp_event(
            tool_name=tool_name,
            status="Failed",
            execution_source="Runtime",
            request_payload=payload,
            error=f"MCP tool {tool_name} was not found or is inactive",
            duration_ms=elapsed_ms(started_ms),
            tool_context=payload if isinstance(payload, dict) else {},
        )
        frappe.throw(_("MCP tool {0} was not found or is inactive").format(tool_name))

    if server_name and row.server and row.server != server_name:
        log_mcp_event(
            tool_name=tool_name,
            status="Failed",
            execution_source="Runtime",
            request_payload=payload,
            error=f"Tool {tool_name} does not belong to server {server_name}",
            duration_ms=elapsed_ms(started_ms),
            tool_meta={
                "url": row.endpoint_url,
                "method": row.http_method,
                "access_mode": row.get("access_mode") or "Read",
            },
            tool_context=payload if isinstance(payload, dict) else {},
        )
        frappe.throw(_("Tool {0} does not belong to server {1}").format(tool_name, server_name))

    try:
        result = _execute_endpoint(row.endpoint_url, row.http_method, payload, tool_name=tool_name)
        log_mcp_event(
            tool_name=tool_name,
            status="Success",
            execution_source="Runtime",
            request_payload=payload,
            response_payload=result,
            duration_ms=elapsed_ms(started_ms),
            tool_meta={
                "url": row.endpoint_url,
                "method": row.http_method,
                "access_mode": row.get("access_mode") or "Read",
            },
            tool_context=payload if isinstance(payload, dict) else {},
        )
        return {"success": True, "result": result}
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub MCP Tool Failure")
        log_mcp_event(
            tool_name=tool_name,
            status="Failed",
            execution_source="Runtime",
            request_payload=payload,
            error=str(exc),
            traceback_text=frappe.get_traceback(),
            duration_ms=elapsed_ms(started_ms),
            tool_meta={
                "url": row.endpoint_url,
                "method": row.http_method,
                "access_mode": row.get("access_mode") or "Read",
            },
            tool_context=payload if isinstance(payload, dict) else {},
        )
        return {"success": False, "error": str(exc), "tool_name": tool_name}


def _execute_endpoint(
    endpoint_url: str,
    http_method: str,
    payload: Dict[str, Any],
    *,
    tool_name: str | None = None,
) -> Any:
    url = (endpoint_url or "").strip()
    if not url:
        frappe.throw(_("endpoint_url is missing on the MCP tool"))

    method = (http_method or "POST").upper()
    if url.startswith("http"):
        if method == "GET":
            response = requests.get(url, params=payload, timeout=30)
        else:
            response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        if "application/json" in (response.headers.get("Content-Type") or ""):
            return response.json()
        return response.text

    fn = frappe.get_attr(url)
    call_payload = dict(payload or {})
    if url == "wa_chat_hub.mcp.configured.execute_configured_tool":
        call_payload["__mcp_tool_name"] = tool_name
    return fn(**call_payload)
