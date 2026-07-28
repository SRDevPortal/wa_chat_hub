from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.agent_router import build_agent_prompt, resolve_agent_route


def _agent(**overrides):
    values = {
        "name": "Patient Care Agent",
        "agent_type": "Patient",
        "system_prompt": "shared",
        "medical_guardrail_policy": "safe",
        "escalation_policy": "handoff",
        "auto_reply_mode": "Draft + Approval",
        "llm_provider": None,
        "max_tool_calls": 2,
        "require_verified_identity": 1,
        "allowed_tools": [frappe._dict(mcp_tool="patient_overview", is_active=1)],
    }
    values.update(overrides)
    return frappe._dict(values)


def _conversation(**overrides):
    values = {
        "linked_patient": "PAT-001",
        "linked_reference_doctype": "Patient",
        "linked_reference_name": "PAT-001",
        "linked_crm_lead": "CRM-001",
        "party_type": "Patient",
        "identity_status": "Matched",
        "medical_department": "Neurology",
        "department_source": "conversation",
        "department_confidence": 1.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestAgentRouter(TestCase):
    @patch("wa_chat_hub.agent_router._department_profile", return_value=None)
    @patch("wa_chat_hub.agent_router._default_agent")
    def test_unverified_patient_receives_no_record_tools(self, default_agent, _department):
        default_agent.return_value = _agent()
        route = resolve_agent_route(_conversation())
        self.assertEqual(route.party_type, "Patient")
        self.assertEqual(route.identity_status, "Matched")
        self.assertEqual(route.allowed_tool_names, set())

    @patch("wa_chat_hub.agent_router._department_profile", return_value=None)
    @patch("wa_chat_hub.agent_router._default_agent")
    def test_patient_classification_overrides_retained_crm_lead(self, default_agent, _department):
        default_agent.return_value = _agent(agent_type="Patient Verification")
        route = resolve_agent_route(
            _conversation(
                linked_patient=None,
                linked_reference_doctype="CRM Lead",
                linked_reference_name="CRM-001",
                identity_status="Ambiguous",
            )
        )
        self.assertEqual(route.party_type, "Patient")
        self.assertEqual(route.agent_type, "Patient Verification")
        self.assertEqual(route.allowed_tool_names, set())

    @patch("wa_chat_hub.agent_router._department_profile", return_value=None)
    @patch("wa_chat_hub.agent_router._default_agent")
    def test_verified_patient_receives_agent_allowlist(self, default_agent, _department):
        default_agent.return_value = _agent()
        route = resolve_agent_route(_conversation(identity_status="Verified"))
        self.assertEqual(route.allowed_tool_names, {"patient_overview"})
        self.assertEqual(route.max_tool_calls, 2)

    @patch("wa_chat_hub.agent_router._department_profile", return_value=None)
    @patch("wa_chat_hub.agent_router._default_agent")
    def test_verification_prompt_requires_only_mobile_and_no_waiting(self, default_agent, _department):
        default_agent.return_value = _agent(agent_type="Patient Verification")
        prompt = build_agent_prompt(resolve_agent_route(_conversation()))
        self.assertIn("registered 10-digit mobile number", prompt)
        self.assertIn("Do not ask for full name, date of birth", prompt)
        self.assertIn("Never say that verification is being processed", prompt)

    @patch("wa_chat_hub.agent_router._department_profile", return_value=None)
    @patch("wa_chat_hub.agent_router._default_agent")
    def test_verified_prompt_resumes_pending_request_immediately(self, default_agent, _department):
        default_agent.return_value = _agent()
        prompt = build_agent_prompt(
            resolve_agent_route(_conversation(identity_status="Verified"))
        )
        self.assertIn("briefly confirm success", prompt)
        self.assertIn("continue the most recent unresolved request", prompt)

    @patch("wa_chat_hub.agent_router._department_profile")
    @patch("wa_chat_hub.agent_router._default_agent")
    def test_department_profile_adds_only_configured_context(self, default_agent, department_profile):
        default_agent.return_value = _agent()
        department_profile.return_value = frappe._dict(
            name="Neurology Patient Profile",
            prompt_overlay="neurology overlay",
            auto_reply_mode="",
            allowed_tools=[frappe._dict(mcp_tool="upcoming_appointments", is_active=1)],
            knowledge_bases=[frappe._dict(knowledge_base="Neurology FAQ", is_active=1)],
        )
        route = resolve_agent_route(_conversation(identity_status="Verified"))
        self.assertEqual(
            route.allowed_tool_names,
            {"patient_overview", "upcoming_appointments"},
        )
        self.assertEqual(route.allowed_knowledge_bases, {"Neurology FAQ"})
        self.assertEqual(route.prompt_overlay, "neurology overlay")

    @patch("wa_chat_hub.api.ai_bot.safe_ai_get_all")
    @patch("wa_chat_hub.api.ai_bot.safe_ai_exists", return_value=True)
    @patch("wa_chat_hub.api.ai_bot.frappe.get_single")
    @patch("wa_chat_hub.api.ai_bot.assert_ai_doctype_permission")
    def test_mcp_tools_are_not_globally_exposed(self, _permission, get_single, _exists, get_all):
        from wa_chat_hub.api.ai_bot import fetch_mcp_tools

        get_single.return_value = frappe._dict(allow_mcp_access=1)
        self.assertEqual(fetch_mcp_tools(), [])
        get_all.assert_not_called()
