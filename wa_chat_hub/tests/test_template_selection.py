import json
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe
from wa_chat_hub.interakt import template_selection as selection


class Account(frappe._dict):
    def set(self, field, value):
        self[field] = [frappe._dict(row) for row in value]


def template(name="welcome", language="en"):
    return {"name": name, "language_code": language, "languages": [language], "status": "APPROVED"}


def response(rows, **metadata):
    result = MagicMock(ok=True)
    result.json.return_value = {"results": {"templates": rows}, **metadata}
    return result


class TestTemplateSelection(TestCase):
    def setUp(self):
        patcher = patch.object(selection, "credential_hash", return_value="test-key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_refresh_preserves_selection_and_disables_missing(self):
        account = Account(is_active=1, connector_status="Active", interakt_template_credential_hash="test-key", interakt_template_catalog=[frappe._dict(template_name="welcome", language_code="en", enabled_in_chat=1), frappe._dict(template_name="removed", enabled_in_chat=1)])
        selection.rebuild_rows(account, [template(), template("new")])
        rows = {row.template_name: row for row in account.interakt_template_catalog}
        self.assertEqual(rows["welcome"].enabled_in_chat, 1)
        self.assertEqual(rows["new"].enabled_in_chat, 0)
        self.assertEqual(rows["removed"].enabled_in_chat, 0)
        self.assertEqual(rows["removed"].approval_status, "Unavailable")

    def test_language_selection_does_not_spread(self):
        account = Account(is_active=1, connector_status="Active", interakt_template_credential_hash="test-key", interakt_template_catalog=[frappe._dict(template_name="welcome", language_code="en", enabled_in_chat=1)])
        selection.rebuild_rows(account, [template(), template(language="hi")])
        self.assertEqual([row.enabled_in_chat for row in account.interakt_template_catalog], [1, 0])

    @patch("wa_chat_hub.security.safe_ai_get_doc")
    def test_empty_selection_does_not_fetch(self, get_doc):
        get_doc.return_value = Account(is_active=1, connector_status="Active", interakt_template_credential_hash="test-key", interakt_template_catalog=[])
        with patch.object(selection, "fetch_live") as fetch:
            self.assertEqual(selection.get_chat_templates("account"), [])
            fetch.assert_not_called()

    @patch("wa_chat_hub.security.safe_ai_get_doc")
    def test_cached_templates_still_respect_current_account_selection(self, get_doc):
        cache = MagicMock()
        cache.get_value.return_value = [template(), template("other"), template(language="hi")]
        get_doc.return_value = Account(is_active=1, connector_status="Active", interakt_template_credential_hash="test-key", interakt_template_catalog=[frappe._dict(template_name="welcome", language_code="en", enabled_in_chat=1, approval_status="Approved")])
        with patch.object(selection.frappe, "cache", return_value=cache):
            self.assertEqual(selection.get_chat_templates("account"), [template()])
        get_doc.assert_called_once_with("Chat Channel Account", "account")
        cache.get_value.assert_called_once_with("wa_interakt_chat_templates::account")

    def test_deselected_template_cannot_be_sent(self):
        with patch.object(selection, "get_chat_templates", return_value=[]), patch.object(selection, "resolve_approved_template") as resolve:
            with self.assertRaises(frappe.ValidationError):
                selection.resolve_chat_template("account", {"template_name": "welcome"})
            resolve.assert_not_called()

    def test_selected_send_uses_filtered_metadata(self):
        rows = [template()]
        with patch.object(selection, "get_chat_templates", return_value=rows), patch.object(selection, "resolve_approved_template", return_value={}) as resolve:
            selection.resolve_chat_template("account", {"template_name": "welcome"})
            self.assertEqual(resolve.call_args.kwargs["approved_templates"], rows)

    def test_failed_refresh_never_saves(self):
        account = MagicMock(channel_type="Interakt")
        with patch.object(selection.frappe, "get_doc", return_value=account), patch.object(selection, "fetch_live", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                selection.refresh_templates("account")
            account.check_permission.assert_called_once_with("write")
            account.save.assert_not_called()

    def test_account_edit_cannot_forge_provider_metadata(self):
        account = Account(channel_type="Interakt", flags=frappe._dict(), interakt_template_snapshot='[{"name":"fake"}]', interakt_template_catalog=[frappe._dict(template_name="fake", enabled_in_chat=1)])
        account.get_doc_before_save = lambda: Account(interakt_template_snapshot=json.dumps([template()]), interakt_template_credential_hash="test-key")
        selection.validate_account(account)
        rows = {row.template_name: row for row in account.interakt_template_catalog}
        self.assertEqual(rows["fake"].enabled_in_chat, 0)
        self.assertEqual(rows["welcome"].enabled_in_chat, 0)

    @patch.object(selection.requests, "get")
    def test_fetches_both_variable_lists_and_pages(self, get):
        get.side_effect = [response([{"name":"a", "approval_status":"APPROVED"}], count=2, has_next=True), response([{"name":"b", "approval_status":"APPROVED"}], count=2, has_next=False), response([], count=0, has_next=False)]
        rows = selection.fetch_live(MagicMock())
        self.assertEqual({row["name"] for row in rows}, {"a", "b"})
        self.assertEqual(get.call_args_list[1].kwargs["params"]["offset"], 1)
        self.assertEqual(get.call_args_list[2].kwargs["params"]["variable_present"], "No")

    @patch.object(selection.requests, "get")
    def test_partial_fetch_fails_instead_of_replacing_catalog(self, get):
        get.side_effect = [response([{"name":"a", "approval_status":"APPROVED"}]), MagicMock(ok=False, status_code=503)]
        with self.assertRaises(frappe.ValidationError):
            selection.fetch_live(MagicMock())

    @patch.object(selection.requests, "get")
    def test_repeated_page_is_rejected(self, get):
        get.return_value = response([{"name":"a", "approval_status":"APPROVED"}], count=3, has_next=True)
        with self.assertRaises(frappe.ValidationError):
            selection.fetch_live(MagicMock())

    @patch.object(selection.requests, "get")
    def test_pending_and_missing_status_not_accepted(self, get):
        get.side_effect = [response([{"name":"pending", "approval_status":"PENDING"}, {"name":"unknown"}]), response([])]
        self.assertEqual(selection.fetch_live(MagicMock()), [])

    def test_changed_key_invalidates_catalog(self):
        account = Account(channel_type="Interakt", flags=frappe._dict(), interakt_template_catalog=[frappe._dict(template_name="welcome", enabled_in_chat=1)])
        account.get_doc_before_save = lambda: Account(interakt_template_snapshot=json.dumps([template()]), interakt_template_credential_hash="old-key")
        selection.validate_account(account)
        self.assertIsNone(account.interakt_template_snapshot)
        self.assertEqual(account.interakt_template_catalog[0].enabled_in_chat, 0)

    @patch("wa_chat_hub.security.safe_ai_get_doc")
    def test_old_catalog_is_not_used_with_another_key(self, get_doc):
        get_doc.return_value = Account(is_active=1, connector_status="Active", interakt_template_credential_hash="old-key")
        self.assertEqual(selection.get_chat_templates("account"), [])

    @patch("wa_chat_hub.security.safe_ai_get_doc")
    def test_picker_uses_saved_catalog_without_network(self, get_doc):
        get_doc.return_value = Account(is_active=1, connector_status="Active", interakt_template_credential_hash="test-key", interakt_template_snapshot=json.dumps([template()]), interakt_template_catalog=[frappe._dict(template_name="welcome", language_code="en", enabled_in_chat=1, approval_status="Approved")])
        cache = MagicMock()
        cache.get_value.return_value = None
        with patch.object(selection.frappe, "cache", return_value=cache), patch.object(selection, "fetch_live", side_effect=RuntimeError("offline")) as fetch:
            self.assertEqual(selection.get_chat_templates("account"), [template()])
            fetch.assert_not_called()
