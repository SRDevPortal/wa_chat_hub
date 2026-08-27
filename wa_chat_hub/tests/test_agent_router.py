from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

import frappe

from wa_chat_hub.agent_router import (
    AgentRoute,
    apply_intent_route,
    build_agent_prompt,
    localized_blocked_reply,
    resolve_agent_route,
)
from wa_chat_hub.ai.configuration import IntentDefinition, IntentRouteDefinition
from wa_chat_hub.tests.policy_fixtures import TEST_POLICY


def _agent(**overrides):
    values = {
        "name": "Patient Care Agent",
        "agent_type": "Patient",
        "is_active": 1,
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
        "channel_account": "Account-1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _intent(name, protected=False):
    return IntentDefinition(
        name=name,
        label=name,
        description=name,
        requires_patient_data=protected,
    )


class TestAgentRouter(TestCase):
    def setUp(self):
        self.policy_patcher = patch(
            "wa_chat_hub.agent_router.get_channel_policy", return_value=TEST_POLICY
        )
        self.policy_patcher.start()
        self.addCleanup(self.policy_patcher.stop)

    @patch("wa_chat_hub.agent_router._department_profile", return_value=None)
    def test_configured_protected_route_uses_verification_profile(self, _department):
        definition = _intent("existing_order_status", protected=True)
        configured = IntentRouteDefinition(
            name="Protected Matched Patient",
            target_type="Agent Profile",
            agent_profile="Patient Verification Agent",
            blocked_reply="Configured verification prompt",
            blocked_replies={
                "default": "English verification prompt",
                "en": "English verification prompt",
                "hi-latn": "Hinglish verification prompt",
                "ta": "Tamil verification prompt",
            },
        )
        with (
            patch("wa_chat_hub.ai.configuration.load_active_intents", return_value={definition.name: definition}),
            patch("wa_chat_hub.ai.configuration.resolve_intent_route", return_value=configured),
            patch(
                "wa_chat_hub.agent_router.frappe.get_doc",
                return_value=_agent(
                    name="Patient Verification Agent",
                    agent_type="Patient Verification",
                    require_verified_identity=0,
                    allowed_tools=[],
                ),
            ),
        ):
            route = AgentRoute(
                party_type="Patient", identity_status="Matched", policy_bundle=TEST_POLICY
            )
            result = apply_intent_route(route, SimpleNamespace(intent=definition.name))
        self.assertEqual(result.agent_type, "Patient Verification")
        self.assertTrue(result.requires_patient_data)
        self.assertEqual(result.blocked_reply, "Configured verification prompt")
        self.assertEqual(result.blocked_replies["en"], "English verification prompt")
        self.assertEqual(result.routing_reason, "configured_intent_route:Protected Matched Patient")

    def test_new_request_uses_channel_default_and_configured_workflow(self):
        definition = _intent("new_treatment_order_request")
        configured = IntentRouteDefinition(
            name="New Treatment Case Workflow",
            ai_workflow="Treatment Case Creation",
        )
        route = AgentRoute(
            agent_profile="Old Profile",
            policy_bundle=TEST_POLICY,
            agent_type="Lead",
            party_type="Patient",
            identity_status="Matched",
            system_prompt="old",
            allowed_tool_names={"profile_tool"},
        )
        with (
            patch("wa_chat_hub.ai.configuration.load_active_intents", return_value={definition.name: definition}),
            patch("wa_chat_hub.ai.configuration.resolve_intent_route", return_value=configured),
        ):
            result = apply_intent_route(route, SimpleNamespace(intent=definition.name))
        self.assertIsNone(result.agent_profile)
        self.assertEqual(result.agent_type, "")
        self.assertEqual(result.ai_workflow, "Treatment Case Creation")
        self.assertEqual(result.allowed_tool_names, set())

    @patch("wa_chat_hub.agent_router._department_profile", return_value=None)
    @patch("wa_chat_hub.agent_router._default_agent")
    def test_unverified_patient_receives_no_record_tools(self, default_agent, _department):
        default_agent.return_value = _agent(agent_type="Patient Verification")
        route = resolve_agent_route(_conversation())
        self.assertEqual(route.party_type, "Patient")
        self.assertEqual(route.identity_status, "Matched")
        self.assertEqual(route.allowed_tool_names, set())

    def test_localized_verification_reply_uses_latest_customer_language(self):
        route = AgentRoute(
            blocked_reply="Legacy fallback",
            policy_bundle=TEST_POLICY,
            blocked_replies={
                "default": "English verification prompt",
                "en": "English verification prompt",
                "hi-latn": "Hinglish verification prompt",
                "ta": "Tamil verification prompt",
            },
        )

        self.assertEqual(
            localized_blocked_reply(route, "tell me about my clinical history"),
            "English verification prompt",
        )
        self.assertEqual(
            localized_blocked_reply(route, "meri clinical history batao"),
            "Hinglish verification prompt",
        )
        self.assertEqual(
            localized_blocked_reply(route, "என் மருத்துவ வரலாற்றை சொல்லுங்கள்"),
            "Tamil verification prompt",
        )

    def test_localized_verification_reply_preserves_legacy_fallback(self):
        route = AgentRoute(blocked_reply="Legacy configured reply")
        self.assertEqual(
            localized_blocked_reply(route, "tell me about my clinical history"),
            "Legacy configured reply",
        )

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
    def test_verification_prompt_requires_customer_number_evidence(self, default_agent, _department):
        default_agent.return_value = _agent(agent_type="Patient Verification")
        route = resolve_agent_route(_conversation())
        route.requires_patient_data = True
        prompt = build_agent_prompt(route)
        self.assertIn("Do not automatically verify", prompt)
        self.assertIn("This is my number", prompt)
        self.assertIn("verify_patient_identity without arguments", prompt)

    def test_matched_patient_public_question_uses_channel_default(self):
        definition = _intent("general_treatment_information", protected=False)
        configured = IntentRouteDefinition(name="Public Channel Default")
        route = AgentRoute(
            agent_profile="Patient Verification Agent",
            policy_bundle=TEST_POLICY,
            agent_type="Patient Verification",
            party_type="Patient",
            identity_status="Matched",
            system_prompt="verification",
            allowed_tool_names={"verify_patient_identity"},
        )
        with (
            patch("wa_chat_hub.ai.configuration.load_active_intents", return_value={definition.name: definition}),
            patch("wa_chat_hub.ai.configuration.resolve_intent_route", return_value=configured),
        ):
            result = apply_intent_route(route, SimpleNamespace(intent=definition.name, requires_patient_data=True))
        self.assertFalse(result.requires_patient_data)
        self.assertIsNone(result.agent_profile)
        self.assertEqual(result.allowed_tool_names, set())
        self.assertIn("do not ask for verification", build_agent_prompt(result))

    @patch("wa_chat_hub.agent_router._department_profile", return_value=None)
    def test_model_cannot_downgrade_configured_protected_intent(self, _department):
        definition = _intent("patient_record_request", protected=True)
        configured = IntentRouteDefinition(
            name="Protected Matched Patient",
            target_type="Agent Profile",
            agent_profile="Patient Verification Agent",
        )
        with (
            patch("wa_chat_hub.ai.configuration.load_active_intents", return_value={definition.name: definition}),
            patch("wa_chat_hub.ai.configuration.resolve_intent_route", return_value=configured),
            patch(
                "wa_chat_hub.agent_router.frappe.get_doc",
                return_value=_agent(name="Patient Verification Agent", agent_type="Patient Verification", require_verified_identity=0),
            ),
        ):
            result = apply_intent_route(
                AgentRoute(
                    party_type="Patient",
                    identity_status="Matched",
                    policy_bundle=TEST_POLICY,
                ),
                SimpleNamespace(intent=definition.name, requires_patient_data=False),
            )
        self.assertTrue(result.requires_patient_data)
        self.assertEqual(result.agent_type, "Patient Verification")

    @patch("wa_chat_hub.agent_router._department_profile", return_value=None)
    @patch("wa_chat_hub.agent_router._default_agent")
    def test_verified_prompt_resumes_pending_request_immediately(self, default_agent, _department):
        default_agent.return_value = _agent()
        prompt = build_agent_prompt(resolve_agent_route(_conversation(identity_status="Verified")))
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
        self.assertEqual(route.allowed_tool_names, {"patient_overview", "upcoming_appointments"})
        self.assertEqual(route.allowed_knowledge_bases, {"Neurology FAQ"})
        self.assertEqual(route.prompt_overlay, "neurology overlay")

    def test_preferred_intent_tool_only_narrows_existing_allowlist(self):
        from wa_chat_hub.api.ai_bot import _apply_configured_intent_tool_narrowing

        route = AgentRoute(
            preferred_mcp_tool="shipping_tool",
            allowed_tool_names={"shipping_tool", "profile_tool"},
            max_tool_calls=3,
        )
        _apply_configured_intent_tool_narrowing(route)
        self.assertEqual(route.allowed_tool_names, {"shipping_tool"})
        self.assertEqual(route.max_tool_calls, 1)

        route.allowed_tool_names = {"profile_tool"}
        _apply_configured_intent_tool_narrowing(route)
        self.assertEqual(route.allowed_tool_names, set())
        self.assertEqual(route.max_tool_calls, 0)

    @patch("wa_chat_hub.api.ai_bot.safe_ai_get_all")
    @patch("wa_chat_hub.api.ai_bot.safe_ai_exists", return_value=True)
    @patch("wa_chat_hub.api.ai_bot.frappe.get_single")
    @patch("wa_chat_hub.api.ai_bot.assert_ai_doctype_permission")
    def test_mcp_tools_are_not_globally_exposed(self, _permission, get_single, _exists, get_all):
        from wa_chat_hub.api.ai_bot import fetch_mcp_tools

        get_single.return_value = frappe._dict(allow_mcp_access=1)
        self.assertEqual(fetch_mcp_tools(), [])
        get_all.assert_not_called()
