from __future__ import annotations

from typing import Any, Dict, List

import frappe


def get_allowed_tools(scope: str = "global", department: str | None = None) -> List[Dict[str, Any]]:
    filters = {"is_active": 1}
    if scope:
        filters["scope"] = scope
    if department:
        filters["department"] = department
    rows = frappe.get_all(
        "WA AI Tool Permission",
        filters=filters,
        fields=["tool_name", "allow_read", "allow_write", "allow_execute", "scope", "department"],
    )
    return rows


def build_mcp_runtime_context(department: str | None = None) -> Dict[str, Any]:
    servers = frappe.get_all(
        "WA MCP Server",
        filters={"is_active": 1},
        fields=["server_label", "transport", "server_url", "auth_type", "scope", "department", "allowed_tools"],
        order_by="modified desc",
    )
    if department:
        servers = [s for s in servers if not s.department or s.department == department]
    return {
        "servers": servers,
        "tools": get_allowed_tools(scope="global") + (get_allowed_tools(scope="department", department=department) if department else []),
    }


def invoke_mcp_tool(server_name: str, tool_name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    # Placeholder for deployment-time wiring to actual MCP runtime.
    return {
        "success": False,
        "message": "MCP runtime invocation must be wired during deployment.",
        "server": server_name,
        "tool": tool_name,
        "payload": payload,
    }
