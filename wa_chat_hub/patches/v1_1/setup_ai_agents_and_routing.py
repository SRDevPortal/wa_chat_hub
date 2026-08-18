from __future__ import annotations


def execute() -> None:
    from wa_chat_hub.security import ensure_default_ai_doctype_permissions
    from wa_chat_hub.setup_agents import (
        ensure_crm_lead_account_mcp_tool,
        ensure_default_agent_profiles,
        ensure_verification_agent_tool,
    )
    from wa_chat_hub.setup_ai_routing import (
        ensure_default_policy_assignment,
        ensure_default_route_blocked_replies,
        seed_default_ai_routing,
    )

    ensure_default_ai_doctype_permissions()
    ensure_default_agent_profiles()
    ensure_verification_agent_tool()
    ensure_crm_lead_account_mcp_tool()
    seed_default_ai_routing()
    ensure_default_policy_assignment()
    ensure_default_route_blocked_replies()
