from __future__ import annotations

import frappe
from frappe import _

from wa_chat_hub.agent_router import persist_agent_route, resolve_agent_route
from wa_chat_hub.identity import reconcile_conversation_identity


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
        "patient": route.patient,
        "department": route.department,
        "department_profile": route.department_profile,
        "routing_reason": route.routing_reason,
        "tools_available": len(route.allowed_tool_names),
        "knowledge_bases": sorted(route.allowed_knowledge_bases),
        "auto_reply_mode": route.auto_reply_mode,
    }


@frappe.whitelist()
def mark_patient_identity_verified(conversation: str, patient: str) -> dict:
    """Administrative bridge until the approved OTP workflow calls this server-side."""
    frappe.only_for("System Manager")
    if not conversation or not patient:
        frappe.throw(_("conversation and patient are required"))
    current = frappe.db.get_value(
        "Chat Conversation",
        conversation,
        ["linked_patient", "linked_reference_doctype", "linked_reference_name"],
        as_dict=True,
    )
    if not current:
        frappe.throw(_("Chat Conversation was not found."))
    linked_patient = current.linked_patient or (
        current.linked_reference_name if current.linked_reference_doctype == "Patient" else None
    )
    if linked_patient and linked_patient != patient:
        frappe.throw(_("The patient does not match the conversation identity."))
    result = reconcile_conversation_identity(
        conversation,
        patient=patient,
        source="approved_identity_verification",
        verified=True,
    )
    route = resolve_agent_route(conversation)
    persist_agent_route(conversation, route)
    return {
        "identity": result,
        "agent_profile": route.agent_profile,
        "identity_status": route.identity_status,
        "tools_available": len(route.allowed_tool_names),
    }


@frappe.whitelist()
def revoke_patient_identity_verification(conversation: str) -> dict:
    frappe.only_for("System Manager")
    if not frappe.db.exists("Chat Conversation", conversation):
        frappe.throw(_("Chat Conversation was not found."))
    frappe.db.set_value(
        "Chat Conversation",
        conversation,
        {
            "identity_status": "Matched",
            "routing_reason": "patient_identity:verification_revoked",
        },
        update_modified=False,
    )
    route = resolve_agent_route(conversation)
    persist_agent_route(conversation, route)
    return {
        "agent_profile": route.agent_profile,
        "identity_status": route.identity_status,
        "tools_available": len(route.allowed_tool_names),
    }
