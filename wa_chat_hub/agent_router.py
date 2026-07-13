from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import frappe


ROUTING_VERSION = 1


@dataclass
class AgentRoute:
    agent_profile: str | None = None
    agent_type: str = "General"
    party_type: str = "Unknown"
    identity_status: str = "Unverified"
    patient: str | None = None
    department: str | None = None
    department_profile: str | None = None
    department_source: str | None = None
    department_confidence: float = 0.0
    routing_reason: str = "legacy_global_configuration"
    system_prompt: str = ""
    medical_guardrail_policy: str = ""
    escalation_policy: str = ""
    prompt_overlay: str = ""
    auto_reply_mode: str | None = None
    llm_provider: str | None = None
    max_tool_calls: int = 0
    allowed_tool_names: set[str] = field(default_factory=set)
    allowed_knowledge_bases: set[str] = field(default_factory=set)

    @property
    def tools_allowed(self) -> bool:
        return bool(self.allowed_tool_names)


def resolve_agent_route(conversation: str | Any) -> AgentRoute:
    convo = frappe.get_doc("Chat Conversation", conversation) if isinstance(conversation, str) else conversation
    party_type, patient = _resolve_party(convo)
    identity_status = getattr(convo, "identity_status", None) or "Unverified"
    department = getattr(convo, "medical_department", None) or None
    department_source = getattr(convo, "department_source", None) or ("conversation" if department else None)
    department_confidence = float(getattr(convo, "department_confidence", None) or (1.0 if department else 0.0))

    requested_type = _agent_type_for_party(party_type, identity_status)
    agent = _default_agent(requested_type)
    if not agent and requested_type == "Patient Verification":
        agent = _default_agent("Patient")
    if not agent:
        agent = _default_agent("General")

    route = AgentRoute(
        agent_profile=agent.name if agent else None,
        agent_type=agent.agent_type if agent else requested_type,
        party_type=party_type,
        identity_status=identity_status,
        patient=patient,
        department=department,
        department_source=department_source,
        department_confidence=department_confidence,
        routing_reason=_routing_reason(party_type, identity_status, bool(agent)),
    )
    if not agent:
        return route

    route.system_prompt = (agent.system_prompt or "").strip()
    route.medical_guardrail_policy = (agent.medical_guardrail_policy or "").strip()
    route.escalation_policy = (agent.escalation_policy or "").strip()
    route.auto_reply_mode = agent.auto_reply_mode or None
    route.llm_provider = agent.llm_provider or None
    route.max_tool_calls = max(0, min(5, int(agent.max_tool_calls or 0)))
    route.allowed_tool_names = _active_child_values(agent.get("allowed_tools"), "mcp_tool")

    department_profile = _department_profile(agent.name, department)
    if department_profile:
        route.department_profile = department_profile.name
        route.prompt_overlay = (department_profile.prompt_overlay or "").strip()
        route.auto_reply_mode = department_profile.auto_reply_mode or route.auto_reply_mode
        route.allowed_tool_names |= _active_child_values(
            department_profile.get("allowed_tools"), "mcp_tool"
        )
        route.allowed_knowledge_bases = _active_child_values(
            department_profile.get("knowledge_bases"), "knowledge_base"
        )

    if (party_type == "Patient" or agent.require_verified_identity) and identity_status != "Verified":
        route.allowed_tool_names.clear()
    return route


def build_agent_prompt(route: AgentRoute) -> str:
    parts = [
        route.system_prompt,
        route.medical_guardrail_policy,
        route.escalation_policy,
        route.prompt_overlay,
    ]
    return "\n\n".join(part for part in parts if part)


def persist_agent_route(conversation: str, route: AgentRoute) -> None:
    meta = frappe.get_meta("Chat Conversation")
    values = {
        "party_type": route.party_type,
        "agent_profile": route.agent_profile,
        "department_source": route.department_source,
        "department_confidence": route.department_confidence,
        "routing_reason": route.routing_reason,
        "routing_version": ROUTING_VERSION,
    }
    values = {key: value for key, value in values.items() if meta.has_field(key)}
    if values:
        frappe.db.set_value("Chat Conversation", conversation, values, update_modified=False)


def _resolve_party(convo) -> tuple[str, str | None]:
    patient = getattr(convo, "linked_patient", None)
    if not patient and getattr(convo, "linked_reference_doctype", None) == "Patient":
        patient = getattr(convo, "linked_reference_name", None)
    if patient:
        return "Patient", patient
    if getattr(convo, "linked_crm_lead", None) or getattr(convo, "linked_reference_doctype", None) in {
        "CRM Lead", "Lead"
    }:
        return "Lead", None
    return getattr(convo, "party_type", None) or "Unknown", None


def _agent_type_for_party(party_type: str, identity_status: str) -> str:
    if party_type == "Patient":
        return "Patient" if identity_status == "Verified" else "Patient Verification"
    if party_type == "Lead":
        return "Lead"
    return "General"


def _default_agent(agent_type: str):
    if not frappe.db.exists("DocType", "WA AI Agent Profile"):
        return None
    name = frappe.db.get_value(
        "WA AI Agent Profile",
        {"agent_type": agent_type, "is_active": 1, "is_default": 1},
        "name",
    )
    return frappe.get_doc("WA AI Agent Profile", name) if name else None


def _department_profile(agent_profile: str, department: str | None):
    if not department or not frappe.db.exists("DocType", "WA AI Department Profile"):
        return None
    name = frappe.db.get_value(
        "WA AI Department Profile",
        {"agent_profile": agent_profile, "medical_department": department, "is_active": 1},
        "name",
        order_by="priority desc, modified desc",
    )
    return frappe.get_doc("WA AI Department Profile", name) if name else None


def _active_child_values(rows, fieldname: str) -> set[str]:
    return {
        str(row.get(fieldname)).strip()
        for row in rows or []
        if row.get("is_active") and row.get(fieldname)
    }


def _routing_reason(party_type: str, identity_status: str, configured: bool) -> str:
    suffix = "configured_profile" if configured else "legacy_global_configuration"
    return f"{party_type.lower()}:{identity_status.lower()}:{suffix}"
