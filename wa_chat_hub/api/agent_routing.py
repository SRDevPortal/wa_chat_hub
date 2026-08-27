from __future__ import annotations

import frappe
from frappe import _

from wa_chat_hub.agent_router import persist_agent_route, resolve_agent_route


@frappe.whitelist()
def get_route(conversation: str) -> dict:
    if not conversation or not frappe.db.exists("Chat Conversation", conversation):
        frappe.throw(_("Chat Conversation was not found."))
    if not frappe.has_permission("Chat Conversation", "read", conversation):
        frappe.throw(_("Not permitted to read this conversation."), frappe.PermissionError)
    route = resolve_agent_route(conversation)
    return {
        "agent_profile": route.agent_profile,
        "agent_type": route.agent_type,
        "party_type": route.party_type,
        "identity_status": route.identity_status,
        "department_profile": route.department_profile,
        "routing_reason": route.routing_reason,
        "tools_available": len(route.allowed_tool_names),
        "knowledge_bases": sorted(route.allowed_knowledge_bases),
        "auto_reply_mode": route.auto_reply_mode,
    }


@frappe.whitelist()
def mark_patient_identity_verified(conversation: str, patient: str) -> dict:
    frappe.throw(_("Patient identity verification is disabled for ShipKia customer flow."))


@frappe.whitelist()
def revoke_patient_identity_verification(conversation: str) -> dict:
    frappe.throw(_("Patient identity verification is disabled for ShipKia customer flow."))
