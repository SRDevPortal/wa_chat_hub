from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from sriaas_clinic.api.patient import validate_unique_contact_mobile
from wa_chat_hub.mcp.configured import (
    _link_existing_contact_to_patient,
    _patient_linked_to_contact,
    _resolve_or_create_patient_from_chat,
    execute_configured_tool,
)
from wa_chat_hub.tests.policy_fixtures import TEST_POLICY


class TestConfiguredEncounterMCP(TestCase):
    def setUp(self):
        self.policy_patcher = patch(
            "wa_chat_hub.mcp.configured.get_conversation_policy",
            return_value=TEST_POLICY,
        )
        self.policy_patcher.start()
        self.addCleanup(self.policy_patcher.stop)
    def test_model_confirmation_flag_cannot_bypass_server_state(self):
        with (
            patch(
                "wa_chat_hub.mcp.configured.safe_ai_get_doc",
                return_value={"execution_config": "{}", "access_mode": "Write"},
            ),
            patch(
                "wa_chat_hub.mcp.configured.frappe.db.exists",
                return_value=False,
            ),
            patch(
                "wa_chat_hub.ai.workflow_engine.validate_workflow_execution",
                side_effect=RuntimeError("pending authorization missing"),
            ) as validate,
            patch("wa_chat_hub.mcp.configured.frappe.new_doc") as new_doc,
        ):
            with self.assertRaisesRegex(RuntimeError, "pending authorization missing"):
                execute_configured_tool(
                    __mcp_tool_name="create_verified_patient_draft_encounter",
                    conversation="CONV-1",
                    customer_confirmed=1,
                    customer_address="House 12, MG Road, Gurgaon, Haryana 122001",
                )

        validate.assert_called_once()
        new_doc.assert_not_called()

    def test_existing_contact_without_patient_keeps_mobile_at_insert(self):
        convo = SimpleNamespace(
            linked_patient=None,
            linked_reference_doctype=None,
            linked_reference_name=None,
            contact="919000000001",
            linked_lead=None,
            department=None,
        )
        patient_doc = Mock()
        patient_doc.name = "PAT-NEW"
        patient_doc.meta.has_field.return_value = True
        patient_doc.meta.fields = []
        values = {}
        patient_doc.get.side_effect = values.get
        patient_doc.set.side_effect = lambda fieldname, value: values.__setitem__(
            fieldname, value
        )
        mobile_at_insert = []
        patient_doc.insert.side_effect = lambda **_kwargs: mobile_at_insert.append(
            values.get("mobile")
        )

        with (
            patch("wa_chat_hub.mcp.configured.safe_ai_get_doc", return_value=convo),
            patch(
                "wa_chat_hub.mcp.configured.frappe.db.exists",
                side_effect=lambda doctype, _name: doctype == "Chat Contact",
            ),
            patch(
                "wa_chat_hub.mcp.configured.frappe.db.get_value",
                return_value="919000000001",
            ),
            patch(
                "wa_chat_hub.mcp.configured._resolve_company",
                return_value="Test Company",
            ),
            patch(
                "wa_chat_hub.mcp.configured._existing_contact_for_phone",
                return_value="CONTACT-1",
            ),
            patch(
                "wa_chat_hub.mcp.configured._patient_linked_to_contact",
                return_value=None,
            ),
            patch(
                "wa_chat_hub.mcp.configured._existing_patient_for_phone",
                return_value=None,
            ),
            patch("wa_chat_hub.mcp.configured.assert_ai_doctype_permission"),
            patch("wa_chat_hub.mcp.configured.frappe.new_doc", return_value=patient_doc),
            patch(
                "wa_chat_hub.mcp.configured._link_existing_contact_to_patient"
            ) as link_contact,
            patch(
                "wa_chat_hub.mcp.configured._link_patient_to_conversation"
            ) as link_conversation,
            patch("wa_chat_hub.mcp.configured.frappe.db.set_value") as set_value,
        ):
            result = _resolve_or_create_patient_from_chat(
                {
                    "create_if_missing": True,
                    "patient_field_values": {
                        "first_name": "Test Patient",
                        "mobile": {"context": "mobile"},
                        "company": {"context": "company"},
                    },
                },
                {"conversation": "CONV-1"},
            )

        self.assertEqual(result, "PAT-NEW")
        self.assertEqual(mobile_at_insert, ["9000000001"])
        self.assertEqual(
            patient_doc.flags.allow_existing_contact_reuse,
            "CONTACT-1",
        )
        set_value.assert_not_called()
        link_contact.assert_called_once_with("CONTACT-1", "PAT-NEW")

        link_conversation.assert_called_once()
    @patch("wa_chat_hub.mcp.configured.frappe.db.exists", return_value=True)
    @patch("wa_chat_hub.mcp.configured.safe_ai_get_doc")
    def test_existing_contact_linked_patient_is_reused(self, get_doc, _exists):
        get_doc.return_value = SimpleNamespace(
            get=lambda fieldname: [
                SimpleNamespace(link_doctype="Patient", link_name="PAT-1")
            ]
            if fieldname == "links"
            else None
        )

        self.assertEqual(_patient_linked_to_contact("CONTACT-1"), "PAT-1")

    @patch("wa_chat_hub.mcp.configured.frappe.db.exists", return_value=True)
    @patch("wa_chat_hub.mcp.configured.safe_ai_get_doc")
    def test_existing_contact_link_is_idempotent(self, get_doc, _exists):
        doc = Mock()
        doc.get.return_value = [
            SimpleNamespace(link_doctype="Patient", link_name="PAT-1")
        ]
        get_doc.return_value = doc

        _link_existing_contact_to_patient("CONTACT-1", "PAT-1")

        doc.append.assert_not_called()
        doc.save.assert_not_called()

    @patch("wa_chat_hub.mcp.configured.safe_ai_get_doc")
    def test_unlinked_contact_gets_patient_dynamic_link(self, get_doc):
        doc = Mock()
        doc.get.return_value = []
        get_doc.return_value = doc

        _link_existing_contact_to_patient("CONTACT-1", "PAT-1")

        doc.append.assert_called_once_with(
            "links",
            {"link_doctype": "Patient", "link_name": "PAT-1"},
        )
        doc.save.assert_called_once_with(ignore_permissions=True)

    @patch("wa_chat_hub.mcp.configured.frappe.db.exists", return_value=False)
    @patch("wa_chat_hub.mcp.configured.safe_ai_get_doc")
    def test_stale_patient_link_is_replaced(self, get_doc, _exists):
        stale_link = SimpleNamespace(
            link_doctype="Patient",
            link_name="PAT-DELETED",
        )
        other_link = SimpleNamespace(
            link_doctype="Lead",
            link_name="LEAD-1",
        )
        doc = Mock()
        doc.get.return_value = [stale_link, other_link]
        get_doc.return_value = doc

        _link_existing_contact_to_patient("CONTACT-1", "PAT-NEW")

        doc.remove.assert_called_once_with(stale_link)
        doc.append.assert_called_once_with(
            "links",
            {"link_doctype": "Patient", "link_name": "PAT-NEW"},
        )
        doc.save.assert_called_once_with(ignore_permissions=True)

    @patch(
        "wa_chat_hub.mcp.configured.frappe.throw",
        side_effect=RuntimeError("Manual review is required"),
    )
    @patch("wa_chat_hub.mcp.configured.frappe.db.exists", return_value=True)
    @patch("wa_chat_hub.mcp.configured.safe_ai_get_doc")
    def test_valid_different_patient_link_is_rejected(
        self,
        get_doc,
        _exists,
        _throw,
    ):
        doc = Mock()
        doc.get.return_value = [
            SimpleNamespace(link_doctype="Patient", link_name="PAT-OTHER")
        ]
        get_doc.return_value = doc

        with self.assertRaisesRegex(RuntimeError, "Manual review"):
            _link_existing_contact_to_patient("CONTACT-1", "PAT-NEW")

        doc.remove.assert_not_called()
        doc.append.assert_not_called()
        doc.save.assert_not_called()

    def test_linked_lead_mobile_precedes_chat_contact_number(self):
        convo = SimpleNamespace(
            linked_patient=None,
            linked_reference_doctype=None,
            linked_reference_name=None,
            contact="CHAT-CONTACT-1",
            linked_lead="LEAD-1",
            department=None,
        )
        lead_values = {
            "first_name": "Test Lead",
            "mobile_no": "9000000001",
        }
        lead_doc = SimpleNamespace(get=lead_values.get)

        with (
            patch(
                "wa_chat_hub.mcp.configured.safe_ai_get_doc",
                side_effect=lambda doctype, _name: (
                    convo if doctype == "Chat Conversation" else lead_doc
                ),
            ),
            patch(
                "wa_chat_hub.mcp.configured.frappe.db.exists",
                side_effect=lambda doctype, _name: doctype
                in {"Chat Contact", "Lead"},
            ),
            patch(
                "wa_chat_hub.mcp.configured.frappe.db.get_value",
                return_value="9111111111",
            ),
            patch(
                "wa_chat_hub.mcp.configured._existing_contact_for_phone",
                return_value=None,
            ) as existing_contact,
            patch(
                "wa_chat_hub.mcp.configured._patient_linked_to_contact",
                return_value=None,
            ),
            patch(
                "wa_chat_hub.mcp.configured._existing_patient_for_phone",
                return_value="PAT-1",
            ),
            patch("wa_chat_hub.mcp.configured._resolve_company"),
            patch("wa_chat_hub.mcp.configured.assert_ai_doctype_permission"),
            patch(
                "wa_chat_hub.mcp.configured._link_patient_to_conversation"
            ),
        ):
            result = _resolve_or_create_patient_from_chat(
                {"link_to_conversation": True},
                {"conversation": "CONV-1"},
            )

        self.assertEqual(result, "PAT-1")
        existing_contact.assert_called_once_with("9000000001")


class TestPatientContactReuseValidation(TestCase):
    def _patient_doc(self, trusted_contact="CONTACT-1"):
        doc = Mock()
        doc.flags = {"allow_existing_contact_reuse": trusted_contact}
        doc.get.side_effect = lambda fieldname: (
            "9000000001" if fieldname == "mobile" else None
        )
        return doc

    def test_exact_trusted_contact_is_reusable(self):
        with (
            patch(
                "sriaas_clinic.api.patient.frappe.db.get_value",
                return_value=None,
            ),
            patch(
                "sriaas_clinic.api.patient.frappe.db.sql",
                return_value=[SimpleNamespace(name="CONTACT-1")],
            ),
        ):
            validate_unique_contact_mobile(self._patient_doc())

    def test_different_contact_is_rejected(self):
        with (
            patch(
                "sriaas_clinic.api.patient.frappe.db.get_value",
                return_value=None,
            ),
            patch(
                "sriaas_clinic.api.patient.frappe.db.sql",
                return_value=[SimpleNamespace(name="CONTACT-2")],
            ),
            patch(
                "sriaas_clinic.api.patient.frappe.throw",
                side_effect=RuntimeError("Contact already exists"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "Contact already exists"):
                validate_unique_contact_mobile(self._patient_doc())

    def test_existing_patient_is_never_bypassed(self):
        with (
            patch(
                "sriaas_clinic.api.patient.frappe.db.get_value",
                return_value="PAT-EXISTING",
            ),
            patch(
                "sriaas_clinic.api.patient.frappe.throw",
                side_effect=RuntimeError("Patient already exists"),
            ),
            patch("sriaas_clinic.api.patient.frappe.db.sql") as contact_lookup,
        ):
            with self.assertRaisesRegex(RuntimeError, "Patient already exists"):
                validate_unique_contact_mobile(self._patient_doc())

        contact_lookup.assert_not_called()

