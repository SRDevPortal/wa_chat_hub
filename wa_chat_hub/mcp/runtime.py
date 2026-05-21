from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
import requests
from frappe import _


def build_mcp_runtime_context(department: Optional[str] = None) -> Dict[str, Any]:
    """Return MCP servers/tools available to the desk runtime."""
    settings = frappe.get_single("WA Chat Hub Settings")
    allow_mcp = bool(getattr(settings, "allow_mcp_access", 0))

    servers = []
    if frappe.db.exists("DocType", "WA MCP Server"):
        filters: Dict[str, Any] = {"is_active": 1}
        if department:
            filters["department"] = ["in", ["", department]]
        servers = frappe.get_all(
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
    if allow_mcp and frappe.db.exists("DocType", "WA MCP Tool Endpoint"):
        tools = frappe.get_all(
            "WA MCP Tool Endpoint",
            filters={"is_active": 1},
            fields=[
                "name",
                "tool_name",
                "description",
                "endpoint_url",
                "http_method",
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
    payload = payload or {}
    if not tool_name:
        frappe.throw(_("tool_name is required"))

    if not frappe.db.exists("DocType", "WA MCP Tool Endpoint"):
        frappe.throw(_("WA MCP Tool Endpoint is not installed"))

    row = frappe.db.get_value(
        "WA MCP Tool Endpoint",
        {"tool_name": tool_name, "is_active": 1},
        ["name", "endpoint_url", "http_method", "server"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("MCP tool {0} was not found or is inactive").format(tool_name))

    if server_name and row.server and row.server != server_name:
        frappe.throw(_("Tool {0} does not belong to server {1}").format(tool_name, server_name))

    try:
        result = _execute_endpoint(row.endpoint_url, row.http_method, payload)
        return {"success": True, "result": result}
    except Exception as exc:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub MCP Tool Failure")
        return {"success": False, "error": str(exc), "tool_name": tool_name}


def _execute_endpoint(endpoint_url: str, http_method: str, payload: Dict[str, Any]) -> Any:
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
    return fn(**payload)
