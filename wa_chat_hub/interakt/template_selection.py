"""Account-specific chat template selection; automation keeps its own policy."""
import json
import hashlib
from urllib.parse import urljoin, urlsplit

import frappe
import requests
from frappe import _
from frappe.utils import cint, now_datetime

from wa_chat_hub.interakt.templates_api import (
    ORGANIZATION_TRACK_TEMPLATES_URL, ORGANIZATION_TRACK_QUERY_BASE,
    _extract_items, _normalize_templates, resolve_approved_template,
)


def credential_hash(account):
    value = account.get("interakt_api_key")
    if value == "":
        return ""
    if value is None or set(str(value)) == {"*"}:
        value = account.get_password("interakt_api_key", raise_exception=False)
    return hashlib.sha256(str(value).encode()).hexdigest() if value else ""


def key(row):
    return (row.get("template_name") or row.get("name") or "", row.get("language_code") or "en")


def fetch_live(account):
    api_key = account.get_password("interakt_api_key")
    if not api_key:
        frappe.throw(_("Configure the Interakt API key before fetching templates."))
    raw = []
    for variable in ("Yes", "No"):
        url = ORGANIZATION_TRACK_TEMPLATES_URL
        params = {**ORGANIZATION_TRACK_QUERY_BASE, "variable_present": variable, "limit": 100, "offset": 0}
        visited = set()
        page_contents = set()
        for page in range(1000):
            marker = (url, json.dumps(params, sort_keys=True))
            if marker in visited:
                frappe.throw(_("Interakt returned repeated pagination. Existing templates were retained."))
            visited.add(marker)
            try:
                response = requests.get(url, headers={"Authorization": f"Basic {api_key}"}, params=params, timeout=25)
            except requests.RequestException:
                frappe.throw(_("Cannot reach Interakt. Check the server connection and try again. Your saved templates have not changed."))
            if not response.ok:
                frappe.throw(_("Interakt template fetch failed (HTTP {0}). Existing templates were retained.").format(response.status_code))
            payload = response.json()
            containers = [payload] if isinstance(payload, dict) else []
            containers += [payload[k] for k in ("data", "results", "pagination", "meta") if isinstance(payload, dict) and isinstance(payload.get(k), dict)]
            list_keys = ("results", "data", "templates", "message_templates", "messageTemplates", "items", "records")
            if not isinstance(payload, list) and not any(isinstance(c.get(k), list) for c in containers for k in list_keys):
                frappe.throw(_("Unrecognized Interakt template response. Existing templates were retained."))
            items = _extract_items(payload)
            fingerprint = json.dumps(items, sort_keys=True)
            if items and fingerprint in page_contents:
                frappe.throw(_("Interakt repeated a template page. Existing templates were retained."))
            page_contents.add(fingerprint)
            raw.extend(items)
            next_url = next((c.get("next") for c in containers if c.get("next")), None)
            total = next((c.get("count", c.get("total")) for c in containers if isinstance(c.get("count", c.get("total")), int)), None)
            has_next = any(c.get("has_next") is True for c in containers)
            if next_url:
                candidate = urljoin(url, str(next_url))
                if urlsplit(candidate).netloc != urlsplit(ORGANIZATION_TRACK_TEMPLATES_URL).netloc or urlsplit(candidate).scheme != "https":
                    frappe.throw(_("Invalid Interakt pagination URL."))
                url, params = candidate, None
            elif params is not None and (has_next or (total is not None and params["offset"] + len(items) < total) or (total is None and len(items) == params["limit"])):
                if not items:
                    frappe.throw(_("Interakt returned an incomplete template list."))
                params = {**params, "offset": params["offset"] + len(items)}
            else:
                break
        else:
            frappe.throw(_("Interakt template pagination did not finish."))
    # Only explicitly approved provider records can enter the chat catalog.
    raw = [row for row in raw if str(row.get("approval_status") or row.get("status") or row.get("template_status") or "").lower() in {"approved", "active", "enabled", "live", "success", "whatsapp_approved"}]
    templates = []
    for row in _normalize_templates(raw):
        for language in row.get("languages") or [row["language_code"]]:
            templates.append({**row, "language_code": language, "languages": [language]})
    return list({key(row): row for row in templates}.values())


def snapshot(account):
    return json.loads(account.get("interakt_template_snapshot") or "[]")


def rebuild_rows(account, templates):
    old = {key(row): row.as_dict() if callable(getattr(row, "as_dict", None)) else dict(row) for row in account.get("interakt_template_catalog") or []}
    rows = []
    for template in templates:
        identity = key(template)
        rows.append({
            "template_name": identity[0], "language_code": identity[1],
            "display_name": template.get("display_name"), "category": template.get("category"),
            "body_preview": template.get("body_preview"),
            "body_variable_count": template.get("body_variable_count", 0),
            "header_variable_count": template.get("header_variable_count", 0),
            "approval_status": "Approved", "enabled_in_chat": cint(old.get(identity, {}).get("enabled_in_chat")),
        })
        old.pop(identity, None)
    for row in old.values():
        rows.append({**{k: row.get(k) for k in ("template_name", "language_code", "display_name", "category", "body_preview", "body_variable_count", "header_variable_count")}, "approval_status": "Unavailable", "enabled_in_chat": 0})
    account.set("interakt_template_catalog", rows)


@frappe.whitelist(methods=["POST"])
def refresh_templates(channel_account):
    account = frappe.get_doc("Chat Channel Account", channel_account)
    account.check_permission("write")
    if account.channel_type != "Interakt":
        frappe.throw(_("This account is not an Interakt account."))
    templates = fetch_live(account)  # Complete both lists before any write.
    account.flags.syncing_interakt_templates = True
    account.interakt_template_credential_hash = credential_hash(account)
    account.interakt_template_snapshot = json.dumps(templates)
    account.interakt_templates_synced_at = now_datetime()
    rebuild_rows(account, templates)
    account.save()
    return {"count": len(templates)}


def validate_account(account):
    if account.flags.syncing_interakt_templates:
        return
    previous = account.get_doc_before_save()
    current_hash = credential_hash(account)
    previous_hash = previous.get("interakt_template_credential_hash") if previous else None
    account.interakt_template_credential_hash = previous_hash
    if not current_hash or current_hash != previous_hash:
        account.interakt_template_snapshot = None
        account.interakt_templates_synced_at = None
        account.interakt_template_credential_hash = None
        rebuild_rows(account, [])
        return

    account.interakt_template_snapshot = previous.get("interakt_template_snapshot") if previous else None
    account.interakt_templates_synced_at = previous.get("interakt_templates_synced_at") if previous else None
    if account.channel_type == "Interakt":
        rebuild_rows(account, snapshot(account))


def clear_cache(account):
    frappe.cache().delete_value(f"wa_interakt_templates::{account.name}")
    frappe.cache().delete_value(f"wa_interakt_chat_templates::{account.name}")


def get_chat_templates(channel_account, force_refresh=False):
    from wa_chat_hub.security import safe_ai_get_doc
    account = safe_ai_get_doc("Chat Channel Account", channel_account)
    if not account.get("is_active") or account.get("connector_status") != "Active":
        return []
    if not credential_hash(account) or credential_hash(account) != account.get("interakt_template_credential_hash"):
        return []
    enabled = {key(row) for row in account.get("interakt_template_catalog") or [] if cint(row.get("enabled_in_chat")) and row.get("approval_status") == "Approved"}
    if not enabled:
        return []
    cache_key = f"wa_interakt_chat_templates::{channel_account}"
    templates = None if force_refresh else frappe.cache().get_value(cache_key)
    if templates is None:
        # Opening the picker uses the last verified catalog, not a network request.
        # Explicit refresh still checks provider approval and reports failures.
        templates = fetch_live(account) if force_refresh else snapshot(account)
        frappe.cache().set_value(cache_key, templates, expires_in_sec=300)
    return [row for row in templates if key(row) in enabled]


def resolve_chat_template(channel_account, template):
    from wa_chat_hub.interakt.templates_api import find_approved_template
    templates = get_chat_templates(channel_account)
    match = find_approved_template(templates, template.get("template_name"), template.get("language_code") or "en")
    if not match:
        frappe.throw(_("This template is not enabled for chat on this account. Refresh the template list or contact your administrator."))
    return resolve_approved_template(channel_account, template, approved_templates=templates)
