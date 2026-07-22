from __future__ import annotations

from typing import Any

import frappe


def log_agent_event(
	event_type: str,
	status: str,
	*,
	company: str | None = None,
	conversation: str | None = None,
	message: str | None = None,
	channel_account: str | None = None,
	provider: str | None = None,
	provider_type: str | None = None,
	model_name: str | None = None,
	tool_name: str | None = None,
	duration_sec: float | None = None,
	http_status: int | None = None,
	request: Any = None,
	response: Any = None,
	error_message: str | None = None,
	traceback: str | None = None,
) -> None:
	"""Best-effort audit event for WhatsApp AI/provider/MCP activity."""
	try:
		if not frappe.db.exists("DocType", "WA Agent Event"):
			return
		doc = frappe.get_doc(
			{
				"doctype": "WA Agent Event",
				"event_type": event_type,
				"status": status,
				"company": company or _conversation_field(conversation, "company"),
				"conversation": conversation,
				"message": message,
				"channel_account": channel_account or _conversation_field(conversation, "channel_account"),
				"provider": provider,
				"provider_type": provider_type,
				"model_name": model_name,
				"tool_name": tool_name,
				"duration_sec": duration_sec,
				"http_status": http_status,
				"request_json": _json(request),
				"response_json": _json(response),
				"error_message": error_message,
				"traceback": traceback,
			}
		)
		doc.insert(ignore_permissions=True)
	except Exception:
		pass


def _conversation_field(conversation: str | None, fieldname: str) -> str | None:
	if not conversation:
		return None
	try:
		return frappe.db.get_value("Chat Conversation", conversation, fieldname)
	except Exception:
		return None


def _json(value: Any) -> str | None:
	if value in (None, ""):
		return None
	try:
		return frappe.as_json(value)
	except Exception:
		return str(value)
