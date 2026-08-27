from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.ai.configuration import IntentRouteDefinition, WorkflowDefinition
from wa_chat_hub.ai.intent_classifier import IntentDecision
from wa_chat_hub.ai.workflow_engine import (
    WorkflowPolicy,
    _challenge_digest,
    evaluate_workflow_policy,
    validate_workflow_execution,
)
from wa_chat_hub.api.ai_bot import (
    _gate_configured_workflow_tools_for_model,
    _handle_configured_workflow,
)


ACTION_TOOL = "configured_write_tool"


def _workflow():
    return WorkflowDefinition(
        name="Configured Case Workflow",
        version=3,
        expiry_minutes=60,
        action_tool=ACTION_TOOL,
        definition={
            "trigger": {"minimum_confidence": 0.55},
            "collect": [
                {
                    "field": "address",
                    "prompt": "Share the configured address.",
                    "invalid_prompt": "Configured address is incomplete.",
                    "validation": {
                        "min_length": 20,
                        "contains_digit": True,
                        "contains_any": ["house", "flat"],
                        "digit_group_lengths_any": [6],
                    },
                }
            ],
            "confirmation": {
                "mode": "challenge",
                "challenge_length": 6,
                "prompt": "Use {address}. Confirmation code: {confirmation_code}",
                "invalid_prompt": "Enter the configured one-time code.",
                "cancel_phrases": ["cancel configured action"],
            },
            "arguments": {
                "reason": {"source": "origin_message"},
                "address": {"source": "collected", "field": "address"},
                "confirmed": {"source": "value", "value": 1},
            },
            "messages": {
                "success": "Configured action completed.",
                "error": "Configured action failed safely.",
                "unavailable": "Configured action is unavailable.",
                "cancelled": "Configured action cancelled.",
            },
        },
    )


class TestGenericWorkflowEngine(TestCase):
    @patch("wa_chat_hub.ai.workflow_engine._save_state")
    @patch("wa_chat_hub.ai.workflow_engine.load_workflow")
    @patch("wa_chat_hub.ai.workflow_engine.load_active_workflow_state", return_value=None)
    def test_configured_intent_route_starts_first_collect_step(self, _state, load, save):
        load.return_value = _workflow()
        policy = evaluate_workflow_policy(
            conversation="CONV-1",
            message_id="MSG-1",
            body_text="start configured action",
            raw_payload={},
            intent_decision=IntentDecision(intent="configured_intent", confidence=0.9),
            intent_route=IntentRouteDefinition(
                name="Route",
                ai_workflow="Configured Case Workflow",
            ),
        )
        self.assertTrue(policy.handled)
        self.assertFalse(policy.execute)
        self.assertEqual(policy.reply, "Share the configured address.")
        self.assertEqual(policy.state["stage"], "collecting")
        save.assert_called_once()

    @patch("wa_chat_hub.ai.workflow_engine._save_state")
    @patch("wa_chat_hub.ai.workflow_engine.load_workflow")
    @patch("wa_chat_hub.ai.workflow_engine.load_active_workflow_state")
    def test_invalid_value_uses_configured_validation_and_prompt(self, load_state, load, save):
        load.return_value = _workflow()
        load_state.return_value = {
            "workflow": "Configured Case Workflow",
            "workflow_version": 3,
            "action_tool": ACTION_TOOL,
            "stage": "collecting",
            "field_index": 0,
            "collected": {},
            "origin_message": "start configured action",
            "instance_id": "instance",
        }
        policy = evaluate_workflow_policy(
            conversation="CONV-1",
            message_id="MSG-2",
            body_text="Aarav from Gurgaon",
            raw_payload={},
            intent_decision=IntentDecision(),
            intent_route=None,
        )
        self.assertEqual(policy.reply, "Configured address is incomplete.")
        save.assert_not_called()

    @patch("wa_chat_hub.ai.workflow_engine.secrets.choice", return_value="7")
    @patch("wa_chat_hub.ai.workflow_engine._save_state")
    @patch("wa_chat_hub.ai.workflow_engine.load_workflow")
    @patch("wa_chat_hub.ai.workflow_engine.load_active_workflow_state")
    def test_valid_value_creates_one_time_confirmation_challenge(
        self, load_state, load, save, _choice
    ):
        load.return_value = _workflow()
        load_state.return_value = {
            "workflow": "Configured Case Workflow",
            "workflow_version": 3,
            "action_tool": ACTION_TOOL,
            "stage": "collecting",
            "field_index": 0,
            "collected": {},
            "origin_message": "start configured action",
            "instance_id": "instance",
        }
        policy = evaluate_workflow_policy(
            conversation="CONV-1",
            message_id="MSG-ADDRESS",
            body_text="House 12, MG Road, Gurgaon 122001",
            raw_payload={},
            intent_decision=IntentDecision(),
            intent_route=None,
        )
        self.assertIn("777777", policy.reply)
        self.assertEqual(policy.state["stage"], "awaiting_confirmation")
        self.assertNotIn("confirmation_code", policy.state)
        save.assert_called_once()

    @patch("wa_chat_hub.ai.workflow_engine._confirmation_prompt_was_delivered", return_value=True)
    @patch("wa_chat_hub.ai.workflow_engine._save_state")
    @patch("wa_chat_hub.ai.workflow_engine.load_workflow")
    @patch("wa_chat_hub.ai.workflow_engine.load_active_workflow_state")
    def test_phone_ownership_text_cannot_authorize_workflow(self, load_state, load, save, _delivered):
        state = {
            "workflow": "Configured Case Workflow",
            "workflow_version": 3,
            "action_tool": ACTION_TOOL,
            "stage": "awaiting_confirmation",
            "collected": {"address": "House 12, MG Road, Gurgaon 122001"},
            "origin_message": "start configured action",
            "instance_id": "instance",
            "confirmation_after_message": "MSG-ADDRESS",
            "confirmation_prompt": "Confirmation code: 777777",
        }
        state["confirmation_digest"] = _challenge_digest(state, "777777")
        load_state.return_value = state
        load.return_value = _workflow()
        policy = evaluate_workflow_policy(
            conversation="CONV-1",
            message_id="MSG-VERIFY",
            body_text="Yahi mera number hai",
            raw_payload={},
            intent_decision=IntentDecision(),
            intent_route=None,
        )
        self.assertFalse(policy.execute)
        self.assertEqual(policy.reply, "Enter the configured one-time code.")
        save.assert_not_called()

    @patch("wa_chat_hub.ai.workflow_engine._confirmation_prompt_was_delivered", return_value=True)
    @patch("wa_chat_hub.ai.workflow_engine.safe_ai_get_all")
    @patch("wa_chat_hub.ai.workflow_engine.load_workflow")
    @patch("wa_chat_hub.ai.workflow_engine.load_active_workflow_state")
    def test_mcp_boundary_accepts_only_latest_matching_challenge(
        self, load_state, load, get_all, _delivered
    ):
        state = {
            "workflow": "Configured Case Workflow",
            "workflow_version": 3,
            "action_tool": ACTION_TOOL,
            "stage": "authorized",
            "collected": {"address": "House 12, MG Road, Gurgaon 122001"},
            "origin_message": "start configured action",
            "instance_id": "instance",
            "confirmation_after_message": "MSG-ADDRESS",
            "confirmation_prompt": "Confirmation code: 777777",
            "confirmation_message": "MSG-CONFIRM",
        }
        state["confirmation_digest"] = _challenge_digest(state, "777777")
        load_state.return_value = state
        load.return_value = _workflow()
        get_all.return_value = [
            {
                "name": "MSG-CONFIRM",
                "body": "777777",
                "raw_payload": None,
                "raw_transport_payload": None,
            }
        ]
        result = validate_workflow_execution(
            "CONV-1",
            ACTION_TOOL,
            {
                "reason": "start configured action",
                "address": "House 12, MG Road, Gurgaon 122001",
                "confirmed": 1,
                "conversation": "CONV-1",
            },
        )
        self.assertEqual(result["confirmation_message"], "MSG-CONFIRM")


class TestGenericWorkflowOrchestration(TestCase):
    def _route(self):
        return SimpleNamespace(
            allowed_tool_names={ACTION_TOOL, "read_tool"},
            max_tool_calls=3,
            patient=None,
            agent_profile=None,
            department=None,
            identity_status="Matched",
            ai_workflow="Configured Case Workflow",
        )

    def test_prerequisite_reply_is_delivered_without_mcp(self):
        policy = WorkflowPolicy(
            handled=True,
            reply="Collect configured field.",
            state={
                "workflow": "Configured Case Workflow",
                "stage": "collecting",
                "action_tool": ACTION_TOOL,
            },
        )
        with (
            patch("wa_chat_hub.api.ai_bot.conversation_update_lock", return_value=nullcontext()),
            patch("wa_chat_hub.api.ai_bot._already_replied_to_inbound", return_value=False),
            patch("wa_chat_hub.api.ai_bot.evaluate_workflow_policy", return_value=policy),
            patch(
                "wa_chat_hub.api.ai_bot.fetch_mcp_tools",
                return_value=[{"function": {"name": ACTION_TOOL}}],
            ),
            patch("wa_chat_hub.api.ai_bot.frappe.db.commit"),
            patch("wa_chat_hub.api.ai_bot._deliver_or_draft_ai_reply", return_value="auto_send") as deliver,
            patch("wa_chat_hub.api.ai_bot.execute_mcp_tool") as execute,
            patch("wa_chat_hub.api.ai_bot.task_log"),
        ):
            handled = _handle_configured_workflow(
                conversation="CONV-1",
                message_id="MSG-1",
                body_text="start",
                raw_payload={},
                intent_decision=IntentDecision(),
                route=self._route(),
                settings=SimpleNamespace(),
            )
        self.assertTrue(handled)
        execute.assert_not_called()
        self.assertEqual(deliver.call_args.args[1], "Collect configured field.")

    def test_missing_account_tool_skips_workflow_and_continues_default_agent(self):
        policy = WorkflowPolicy(
            handled=True,
            reply="Collect configured field.",
            state={
                "workflow": "Configured Case Workflow",
                "stage": "collecting",
                "action_tool": ACTION_TOOL,
            },
        )
        route = self._route()
        route.allowed_tool_names = {"read_tool"}
        with (
            patch("wa_chat_hub.api.ai_bot.conversation_update_lock", return_value=nullcontext()),
            patch("wa_chat_hub.api.ai_bot._already_replied_to_inbound", return_value=False),
            patch("wa_chat_hub.api.ai_bot.evaluate_workflow_policy", return_value=policy),
            patch("wa_chat_hub.api.ai_bot.fetch_mcp_tools") as fetch_tools,
            patch("wa_chat_hub.api.ai_bot.clear_active_workflow") as clear,
            patch("wa_chat_hub.api.ai_bot.frappe.db.commit"),
            patch("wa_chat_hub.api.ai_bot._deliver_or_draft_ai_reply") as deliver,
            patch("wa_chat_hub.api.ai_bot.execute_mcp_tool") as execute,
            patch("wa_chat_hub.api.ai_bot.task_log"),
        ):
            handled = _handle_configured_workflow(
                conversation="CONV-1",
                message_id="MSG-1",
                body_text="start",
                raw_payload={},
                intent_decision=IntentDecision(),
                route=route,
                settings=SimpleNamespace(),
            )
        self.assertFalse(handled)
        clear.assert_called_once_with("CONV-1")
        fetch_tools.assert_not_called()
        deliver.assert_not_called()
        execute.assert_not_called()

    def test_mcp_failure_clears_state_and_uses_configured_error(self):
        policy = WorkflowPolicy(
            handled=True,
            execute=True,
            tool_name=ACTION_TOOL,
            arguments={"confirmed": 1},
            state={"workflow": "Configured Case Workflow", "stage": "authorized"},
        )
        with (
            patch("wa_chat_hub.api.ai_bot.conversation_update_lock", return_value=nullcontext()),
            patch("wa_chat_hub.api.ai_bot._already_replied_to_inbound", return_value=False),
            patch("wa_chat_hub.api.ai_bot.evaluate_workflow_policy", return_value=policy),
            patch("wa_chat_hub.api.ai_bot.fetch_mcp_tools", return_value=[{"function": {"name": ACTION_TOOL}}]),
            patch("wa_chat_hub.api.ai_bot.execute_mcp_tool", return_value="Error: safe failure"),
            patch("wa_chat_hub.api.ai_bot.clear_active_workflow") as clear,
            patch("wa_chat_hub.api.ai_bot.workflow_message", return_value="Configured action failed safely."),
            patch("wa_chat_hub.api.ai_bot.frappe.db.commit"),
            patch("wa_chat_hub.api.ai_bot._deliver_or_draft_ai_reply", return_value="auto_send") as deliver,
            patch("wa_chat_hub.api.ai_bot.task_log"),
        ):
            handled = _handle_configured_workflow(
                conversation="CONV-1",
                message_id="MSG-CONFIRM",
                body_text="777777",
                raw_payload={},
                intent_decision=IntentDecision(),
                route=self._route(),
                settings=SimpleNamespace(),
            )
        self.assertTrue(handled)
        clear.assert_called_once_with("CONV-1")
        self.assertEqual(deliver.call_args.args[1], "Configured action failed safely.")

    @patch("wa_chat_hub.api.ai_bot.active_workflow_action_tools", return_value={ACTION_TOOL})
    def test_model_gate_removes_all_configured_workflow_tools(self, _tools):
        route = self._route()
        _gate_configured_workflow_tools_for_model(route)
        self.assertEqual(route.allowed_tool_names, {"read_tool"})
        self.assertEqual(route.max_tool_calls, 3)
