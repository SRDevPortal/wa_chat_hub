from types import SimpleNamespace
from unittest import TestCase

from wa_chat_hub.api.ai_bot import _route_reference_party_type
from wa_chat_hub.policy import PolicyBundle


class TestAIBotPolicyRouting(TestCase):
    def test_patient_party_type_comes_from_assigned_policy(self):
        policy = PolicyBundle(
            name="Custom Policy",
            version=1,
            sections={
                "party_routing_policy": {
                    "party_type_by_reference_doctype": {"Patient": "Care Recipient"}
                }
            },
        )
        route = SimpleNamespace(policy_bundle=policy)

        self.assertEqual(
            _route_reference_party_type(route, "Patient"),
            "Care Recipient",
        )

    def test_missing_policy_has_no_implicit_patient_party_type(self):
        route = SimpleNamespace(policy_bundle=None)

        self.assertEqual(_route_reference_party_type(route, "Patient"), "")
