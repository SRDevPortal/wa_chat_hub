from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from wa_chat_hub.ai.configuration import (
    IntentDefinition,
    build_classifier_system_prompt,
    match_configured_intent,
)
from wa_chat_hub.ai.intent_classifier import (
    classify_customer_intent,
    classify_customer_intent_fallback,
    needs_patient_verification_question,
    should_attempt_patient_verification,
)

from wa_chat_hub.tests.policy_fixtures import TEST_POLICY

def _catalog():
    return {
        "new_treatment_order_request": IntentDefinition(
            name="new_treatment_order_request",
            label="New treatment order",
            description="Create a new treatment case or order.",
            examples=("order ceate krdo", "dawai bhej do"),
            fallback_match={"contains_any": ["order create", "order ceate", "new order", "dawai bhej"]},
            priority=100,
            confidence_threshold=0.55,
        ),
        "existing_order_status": IntentDefinition(
            name="existing_order_status",
            label="Existing shipment",
            description="Track an existing order or shipment.",
            examples=("awb tracking", "order status"),
            fallback_match={"contains_any": ["awb", "tracking", "order status", "order kaha"]},
            priority=80,
            confidence_threshold=0.6,
            requires_patient_data=True,
        ),
        "patient_record_request": IntentDefinition(
            name="patient_record_request",
            label="Patient records",
            description="Read this patient's stored records.",
            examples=("meri prescription bhejo",),
            fallback_match={"contains_any": ["meri prescription", "clinical history"]},
            priority=75,
            confidence_threshold=0.6,
            requires_patient_data=True,
        ),
        "general_treatment_information": IntentDefinition(
            name="general_treatment_information",
            label="General treatment information",
            description="Public information that needs no patient record.",
            examples=("psoriasis ke treatments kya hain",),
            fallback_match={"contains_any": ["psoriasis", "treatment"]},
            priority=20,
            confidence_threshold=0.5,
        ),
        "greeting_or_general_chat": IntentDefinition(
            name="greeting_or_general_chat",
            label="Greeting",
            description="Greeting or normal public chat.",
            examples=("hello",),
            fallback_match={"full_text_any": ["hi", "hello"]},
            priority=60,
            confidence_threshold=0.5,
        ),
        "unclear": IntentDefinition(
            name="unclear",
            label="Unclear",
            description="Not enough information to route safely.",
            priority=0,
            confidence_threshold=0,
            is_default_fallback=True,
            clarification_prompt="Please clarify your request.",
        ),
    }


class TestConfiguredIntentFallback(TestCase):
    @patch("wa_chat_hub.ai.intent_classifier.load_active_intents", side_effect=_catalog)
    def test_problem_message_routes_to_new_treatment_order(self, _load):
        result = classify_customer_intent_fallback(
            "mujhe treatment lena hai mera order ceate krdo",
            party_type="Lead",
        )
        self.assertEqual(result.intent, "new_treatment_order_request")
        self.assertFalse(result.should_call_mcp)
        self.assertIsNone(result.mcp_tool)

    @patch("wa_chat_hub.ai.intent_classifier.load_active_intents", side_effect=_catalog)
    def test_creation_priority_wins_over_tracking_word(self, _load):
        result = classify_customer_intent_fallback(
            "new order create karo, tracking baad me dena"
        )
        self.assertEqual(result.intent, "new_treatment_order_request")

    @patch("wa_chat_hub.ai.intent_classifier.load_active_intents", side_effect=_catalog)
    def test_explicit_tracking_is_protected(self, _load):
        result = classify_customer_intent_fallback("awb number se tracking batao")
        self.assertEqual(result.intent, "existing_order_status")
        self.assertTrue(result.requires_patient_data)

    @patch("wa_chat_hub.ai.intent_classifier.load_active_intents", side_effect=_catalog)
    def test_ambiguous_order_uses_configured_fallback(self, _load):
        result = classify_customer_intent_fallback("mera order")
        self.assertEqual(result.intent, "unclear")

    def test_changing_configuration_changes_behavior_without_code(self):
        catalog = _catalog()
        catalog["new_treatment_order_request"] = IntentDefinition(
            **{
                **catalog["new_treatment_order_request"].__dict__,
                "fallback_match": {"contains_any": ["callback arrange"]},
            }
        )
        self.assertEqual(
            match_configured_intent("callback arrange please", catalog).name,
            "new_treatment_order_request",
        )

    def test_classifier_prompt_is_built_from_catalog(self):
        prompt = build_classifier_system_prompt(_catalog())
        self.assertIn("new_treatment_order_request", prompt)
        self.assertIn("order ceate krdo", prompt)
        self.assertIn("Do not select an agent, tool", prompt)


class TestConfiguredIntentProvider(TestCase):
    def _provider(self):
        return SimpleNamespace(
            provider_type="OpenAI",
            model_name="test-model",
            base_url="https://example.test/v1/chat/completions",
            api_key="secret",
        )

    @patch("wa_chat_hub.ai.intent_classifier.load_active_intents", side_effect=_catalog)
    @patch("wa_chat_hub.ai.intent_classifier.requests.post")
    def test_invalid_json_falls_back_safely(self, post, _load):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"message": {"content": "not-json"}}]}
        post.return_value = response
        result = classify_customer_intent(self._provider(), "mera order", timeout=20)
        self.assertEqual(result.intent, "unclear")
        self.assertEqual(result.source, "configured_fallback")

    @patch("wa_chat_hub.ai.intent_classifier.load_active_intents", side_effect=_catalog)
    @patch("wa_chat_hub.ai.intent_classifier.requests.post")
    def test_configured_creation_match_overrides_wrong_model_tracking(self, post, _load):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": '{"intent":"existing_order_status","confidence":0.99,"reason":"wrong"}'}}]
        }
        post.return_value = response
        result = classify_customer_intent(
            self._provider(),
            "treatment ke liye order create kar do",
            timeout=20,
        )
        self.assertEqual(result.intent, "new_treatment_order_request")

    @patch("wa_chat_hub.ai.intent_classifier.load_active_intents", side_effect=_catalog)
    @patch("wa_chat_hub.ai.intent_classifier.requests.post")
    def test_model_agent_tool_and_privacy_fields_are_ignored(self, post, _load):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"agent":"sales_agent","intent":"patient_record_request",'
                            '"confidence":0.99,"requires_patient_data":false,'
                            '"should_call_mcp":true,"mcp_tool":"dangerous_write",'
                            '"reason":"model recommendation"}'
                        )
                    }
                }
            ]
        }
        post.return_value = response
        result = classify_customer_intent(self._provider(), "please help with records", timeout=20)
        self.assertEqual(result.intent, "patient_record_request")
        self.assertTrue(result.requires_patient_data)
        self.assertFalse(result.should_call_mcp)
        self.assertIsNone(result.mcp_tool)
        self.assertEqual(result.agent, "configured")


class TestPatientVerificationGate(TestCase):
    def test_public_matched_conversation_does_not_ask_for_verification(self):
        route = SimpleNamespace(
            party_type="Patient",
            identity_status="Matched",
            requires_patient_data=False,
            policy_bundle=TEST_POLICY,
        )
        self.assertFalse(needs_patient_verification_question(route, "Hi"))

    def test_protected_matched_request_asks_for_verification(self):
        route = SimpleNamespace(
            party_type="Patient",
            identity_status="Matched",
            requires_patient_data=True,
            policy_bundle=TEST_POLICY,
        )
        self.assertTrue(needs_patient_verification_question(route, "meri prescription bhejo"))

    def test_current_number_claim_reaches_identity_verification_flow(self):
        route = SimpleNamespace(
            party_type="Patient",
            identity_status="Matched",
            requires_patient_data=True,
            policy_bundle=TEST_POLICY,
        )
        self.assertFalse(needs_patient_verification_question(route, "Yahi mera number hai"))
    def test_registered_number_independently_attempts_verification(self):
        route = SimpleNamespace(
            party_type="Patient",
            identity_status="Matched",
            requires_patient_data=True,
            policy_bundle=TEST_POLICY,
        )
        self.assertTrue(
            should_attempt_patient_verification(route, "9000000001")
        )
        self.assertFalse(
            needs_patient_verification_question(route, "9000000001")
        )

    def test_current_number_claim_independently_attempts_verification(self):
        route = SimpleNamespace(
            party_type="Patient",
            policy_bundle=TEST_POLICY,
            identity_status="Matched",
            requires_patient_data=True,
        )
        self.assertTrue(
            should_attempt_patient_verification(route, "This is my number")
        )
        self.assertFalse(
            needs_patient_verification_question(route, "This is my number")
        )

    def test_verification_is_not_attempted_without_either_option(self):
        route = SimpleNamespace(
            party_type="Patient",
            identity_status="Matched",
            requires_patient_data=True,
            policy_bundle=TEST_POLICY,
        )
        self.assertFalse(
            should_attempt_patient_verification(route, "send my encounter")
        )
        self.assertTrue(
            needs_patient_verification_question(route, "send my encounter")
        )
