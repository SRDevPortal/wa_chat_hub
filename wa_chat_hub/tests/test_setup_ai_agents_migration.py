from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.patches.v1_1.setup_ai_agents_and_routing import execute


class TestSetupAIAgentsMigration(TestCase):
    def test_shipping_tool_exists_before_routing_is_seeded(self):
        calls = []

        def record(name):
            return lambda: calls.append(name)

        with (
            patch(
                "wa_chat_hub.security.ensure_default_ai_doctype_permissions",
                side_effect=record("permissions"),
            ),
            patch(
                "wa_chat_hub.setup_agents.ensure_default_agent_profiles",
                side_effect=record("profiles"),
            ),
            patch(
                "wa_chat_hub.setup_agents.ensure_verification_agent_tool",
                side_effect=record("verification_tool"),
            ),
            patch(
                "wa_chat_hub.setup_agents.ensure_crm_lead_account_mcp_tool",
                side_effect=record("crm_tool"),
            ),
            patch(
                "wa_chat_hub.setup_agents.ensure_patient_shipping_history_tool",
                side_effect=record("shipping_tool"),
            ),
            patch(
                "wa_chat_hub.setup_ai_routing.seed_default_ai_routing",
                side_effect=record("routing"),
            ),
            patch(
                "wa_chat_hub.setup_ai_routing.ensure_default_policy_assignment",
                side_effect=record("policy_assignment"),
            ),
            patch(
                "wa_chat_hub.setup_ai_routing.ensure_default_route_blocked_replies",
                side_effect=record("blocked_replies"),
            ),
        ):
            execute()

        self.assertLess(calls.index("shipping_tool"), calls.index("routing"))
        self.assertEqual(
            calls,
            [
                "permissions",
                "profiles",
                "verification_tool",
                "crm_tool",
                "shipping_tool",
                "routing",
                "policy_assignment",
                "blocked_replies",
            ],
        )
