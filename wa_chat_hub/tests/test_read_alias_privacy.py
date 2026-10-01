import unittest
from unittest.mock import patch

import frappe
from wa_chat_hub import hooks, client_privacy, desk_privacy
from privacy_shield import desk, listing


class ReadAliasPrivacyTests(unittest.TestCase):
    def aliases(self):
        return [(source, target) for source, target in hooks.override_whitelisted_methods.items()
                if source.startswith(("privacy_shield.", "sriaas_clinic.api.crm_lead.privacy."))
                and target.startswith(("wa_chat_hub.client_privacy.", "wa_chat_hub.desk_privacy."))]

    def invoke(self, target, doctype, raw, denied=False):
        action = target.rsplit(".", 1)[1]
        endpoint = frappe.get_attr(target)
        module = desk if action in ("get", "getdoc") else listing
        if action in ("get", "getdoc"):
            args = (doctype, "1042")
        elif action == "get_list":
            args = (doctype,)
        elif action == "get_value":
            args = (doctype, "contact", "1042")
        else:
            args = ()
        returned = [raw] if action in ("get_list", "reportview_get", "reportview_get_list") else raw
        with patch.object(module, action, return_value=returned,
                          side_effect=frappe.PermissionError if denied else None) as original:
            value = endpoint(*args)
            original.assert_called_once()
            self.assertEqual(original.call_args.args[:len(args)], args)
        return frappe.response.docs if action == "getdoc" else value

    def test_twelve_aliases_mask_without_mutating_source(self):
        aliases = self.aliases()
        self.assertEqual(len(aliases), 12)
        for source, target in aliases:
            raw = {"doctype": "Chat Conversation", "name": "1042", "contact": "9876501234"}
            with self.subTest(alias=source), \
                 patch.object(client_privacy, "restricted", return_value=True), \
                 patch.object(desk_privacy, "restricted", return_value=True), \
                 patch.object(frappe.local, "form_dict", frappe._dict(doctype="Chat Conversation")), \
                 patch.object(frappe.local, "response", frappe._dict(docs=[raw])):
                visible = self.invoke(target, "Chat Conversation", raw)
                self.assertNotIn("9876501234", str(visible))
                self.assertEqual(raw["contact"], "9876501234")

    def test_non_chat_and_full_visibility_responses_unchanged(self):
        for doctype, limited in (("Customer", True), ("Patient", True), ("CRM Lead", True),
                                 ("Chat Conversation", False), ("Chat Contact", False)):
            for source, target in self.aliases():
                raw = {"doctype": doctype, "name": "1042", "contact": "9876501234"}
                with self.subTest(alias=source, doctype=doctype), \
                     patch.object(client_privacy, "restricted", return_value=limited), \
                     patch.object(desk_privacy, "restricted", return_value=limited), \
                     patch.object(frappe.local, "form_dict", frappe._dict(doctype=doctype)), \
                     patch.object(frappe.local, "response", frappe._dict(docs=[raw])):
                    visible = self.invoke(target, doctype, raw)
                    if isinstance(visible, list):
                        self.assertIs(visible[0], raw)
                    else:
                        self.assertIs(visible, raw)

    def test_permission_denials_preserved_for_every_alias(self):
        for source, target in self.aliases():
            with self.subTest(alias=source), self.assertRaises(frappe.PermissionError):
                self.invoke(target, "Chat Conversation", {}, denied=True)
