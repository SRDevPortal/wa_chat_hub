import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from wa_chat_hub.setup_agents import _upsert_patient_draft_encounter_endpoint


class TestInsertOnlyAISetup(TestCase):
    @patch("wa_chat_hub.setup_agents.frappe.get_doc")
    @patch("wa_chat_hub.setup_agents.frappe.db.exists", return_value=True)
    def test_existing_endpoint_is_never_synchronized_or_reactivated(self, _exists, get_doc):
        self.assertFalse(_upsert_patient_draft_encounter_endpoint())
        get_doc.assert_not_called()

    def test_business_workflow_defaults_live_in_declarative_bootstrap_data(self):
        path = Path(__file__).resolve().parents[1] / "config" / "default_ai_routing.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        workflows = {row["workflow_name"]: row for row in data["WA AI Workflow"]}
        definition = workflows["Treatment Case Creation"]["definition"]
        self.assertEqual(definition["arguments"]["encounter_type"]["value"], "Followup")
        self.assertEqual(definition["arguments"]["encounter_place"]["value"], "Online")
        self.assertEqual(definition["collect"][0]["field"], "customer_address")
