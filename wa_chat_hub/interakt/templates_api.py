"""Fetch approved WhatsApp templates from Interakt for WA Chat Hub."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import frappe
import requests
from frappe import _

from wa_chat_hub.security import safe_ai_get_doc, safe_ai_get_value

ORG_ID_PATTERN = re.compile(
    r"/organizations/([0-9a-fA-F-]{36})/message-templates",
    re.IGNORECASE,
)
APPROVED_STATUSES = frozenset(
    {"approved", "active", "enabled", "live", "success", "whatsapp_approved"}
)
LEGACY_TEMPLATE_ENDPOINTS = (
    "https://api.interakt.ai/v1/public/track/whatsapp/templates/",
    "https://api.interakt.ai/v1/public/templates/",
)
ORGANIZATION_TRACK_TEMPLATES_URL = (
    "https://api.interakt.ai/v1/public/track/organization/templates"
)
ORGANIZATION_TRACK_QUERY_BASE = {
    "template_name": "",
    "autosubmitted_for": "all",
    "approval_status": "APPROVED",
    "language": "all",
}
CACHE_TTL = 300
V2_TEMPLATE_PATH = "message-templates/v2"

API_LIST_UNAVAILABLE_MSG = _(
    "Could not load approved templates from Interakt. Confirm the API key under Chat Channel Account, "
    "or add rows under Approved Templates (manual catalog) using names from Interakt → Templates → Active."
)


def fetch_approved_templates(channel_account: str, force_refresh: bool = False) -> List[Dict[str, Any]]:
    account = safe_ai_get_doc("Chat Channel Account", channel_account)
    if account.channel_type != "Interakt":
        frappe.throw(_("Templates are only available for Interakt channel accounts"))

    manual = _load_manual_catalog(account)
    cache_key = f"wa_interakt_templates::{channel_account}"
    if not force_refresh:
        cached = frappe.cache().get_value(cache_key)
        if cached is not None:
            return _merge_template_lists(cached, manual)

    api_key = account.get_password("interakt_api_key")
    if not api_key and not manual:
        frappe.throw(_("Interakt API Key is not configured on {0}").format(account.name))

    api_templates: List[Dict[str, Any]] = []
    last_error: Optional[str] = None

    if api_key:
        try:
            raw_items = _fetch_organization_track_templates(api_key)
            api_templates = _normalize_templates(raw_items)
        except Exception as exc:
            last_error = str(exc)
            frappe.log_error(
                frappe.get_traceback(),
                "Interakt Organization Track Templates Fetch Failed",
            )

        if not api_templates:
            endpoints = _build_template_list_urls(account)
            for url in endpoints:
                if url.rstrip("/") == ORGANIZATION_TRACK_TEMPLATES_URL:
                    continue
                try:
                    raw_items = _request_templates(url, api_key)
                    api_templates = _normalize_templates(raw_items)
                    if api_templates:
                        break
                except Exception as exc:
                    last_error = str(exc)
                    frappe.log_error(
                        frappe.get_traceback(),
                        f"Interakt Templates Fetch Failed: {url}",
                    )

    merged = _merge_template_lists(api_templates, manual)
    if merged:
        frappe.cache().set_value(cache_key, merged, expires_in_sec=CACHE_TTL)
        return merged

    if last_error:
        frappe.throw(
            _("{0} Last API error: {1}").format(API_LIST_UNAVAILABLE_MSG, last_error)
        )
    if not manual:
        frappe.throw(API_LIST_UNAVAILABLE_MSG)
    return manual


def resolve_approved_template(
    channel_account: str,
    template: Dict[str, Any],
) -> Dict[str, Any]:
    template_name = (template.get("template_name") or "").strip()
    language_code = (template.get("language_code") or "en").strip() or "en"
    if not template_name:
        frappe.throw(_("Template name is required."))

    approved_templates = fetch_approved_templates(channel_account, force_refresh=False)
    match = find_approved_template(approved_templates, template_name, language_code)
    if not match:
        approved_templates = fetch_approved_templates(channel_account, force_refresh=True)
        match = find_approved_template(approved_templates, template_name, language_code)
    if not match:
        available = ", ".join(
            sorted(
                {
                    f"{row.get('name')} ({row.get('language_code') or 'en'})"
                    for row in approved_templates
                    if row.get("name")
                }
            )
        )
        frappe.throw(
            _(
                "WhatsApp template '{0}' language '{1}' is not approved on channel '{2}'. "
                "Approved templates available: {3}."
            ).format(template_name, language_code, channel_account, available or _("none"))
        )

    body_values = template.get("body_values") or []
    variable_count = int(match.get("body_variable_count") or len(match.get("body_variables") or []))
    if variable_count != len(body_values):
        frappe.throw(
            _(
                "WhatsApp template '{0}' expects {1} body variables, but {2} were provided."
            ).format(match.get("name") or template_name, variable_count, len(body_values))
        )

    resolved_name = (match.get("name") or template_name).strip()
    if resolved_name == template_name:
        return template
    return {
        **template,
        "template_name": resolved_name,
        "configured_template_name": template_name,
    }


def find_approved_template(
    templates: List[Dict[str, Any]],
    template_name: str,
    language_code: str,
) -> Optional[Dict[str, Any]]:
    wanted_name = (template_name or "").strip().lower()
    wanted_language = (language_code or "en").strip().lower() or "en"
    display_match = None

    for row in templates or []:
        name = (row.get("name") or "").strip()
        display_name = (row.get("display_name") or "").strip()
        languages = row.get("languages") or [row.get("language_code") or "en"]
        available_languages = {
            str(language or "en").strip().lower() or "en" for language in languages
        }
        if wanted_language not in available_languages:
            continue
        if name.lower() == wanted_name:
            return row
        if display_name.lower() == wanted_name and not display_match:
            display_match = row

    return display_match


def _load_manual_catalog(account) -> List[Dict[str, Any]]:
    rows = getattr(account, "interakt_template_catalog", None) or []
    templates: List[Dict[str, Any]] = []
    for row in rows:
        name = (getattr(row, "template_name", None) or "").strip()
        if not name:
            continue
        display_name = (getattr(row, "display_name", None) or "").strip() or name.replace("_", " ").title()
        templates.append(
            {
                "name": name,
                "display_name": display_name,
                "language_code": (getattr(row, "language_code", None) or "en").strip() or "en",
                "languages": [(getattr(row, "language_code", None) or "en").strip() or "en"],
                "category": (getattr(row, "category", None) or "").strip(),
                "status": "approved",
                "body_preview": (getattr(row, "body_preview", None) or "").strip(),
                "header_preview": "",
                "body_variable_count": int(getattr(row, "body_variable_count", None) or 0),
                "header_variable_count": int(getattr(row, "header_variable_count", None) or 0),
                "has_variables": bool(int(getattr(row, "body_variable_count", None) or 0))
                or bool(int(getattr(row, "header_variable_count", None) or 0)),
                "source": "manual",
            }
        )
    for item in templates:
        body_preview = item.get("body_preview") or ""
        header_preview = item.get("header_preview") or ""
        body_count = int(item.get("body_variable_count") or 0)
        header_count = int(item.get("header_variable_count") or 0)
        item["body_variables"] = _extract_variable_slots(body_preview, body_count, "body")
        item["header_variables"] = _extract_variable_slots(header_preview, header_count, "header")
        if not item.get("has_variables"):
            item["has_variables"] = bool(item["body_variables"] or item["header_variables"])
    templates.sort(key=lambda item: (item.get("display_name") or item.get("name") or "").lower())
    return templates


def _merge_template_lists(
    primary: List[Dict[str, Any]], secondary: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    for row in (primary or []) + (secondary or []):
        name = (row.get("name") or "").strip()
        lang = (row.get("language_code") or "en").strip() or "en"
        key = (name, lang)
        if not name or key in seen:
            continue
        seen.add(key)
        merged.append(row)
    merged.sort(key=lambda item: (item.get("display_name") or item.get("name") or "").lower())
    return merged


def _fetch_organization_track_templates(api_key: str) -> List[Dict[str, Any]]:
    """List approved org templates via Interakt track API (Yes + No variable_present)."""
    headers = {
        "Authorization": f"Basic {api_key}",
        "Content-Type": "application/json",
    }
    merged: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()
    last_error: Optional[str] = None

    for variable_present in ("Yes", "No"):
        params = {**ORGANIZATION_TRACK_QUERY_BASE, "variable_present": variable_present}
        response = requests.get(
            ORGANIZATION_TRACK_TEMPLATES_URL,
            headers=headers,
            params=params,
            timeout=25,
        )
        if not response.ok:
            detail = (response.text or "")[:500]
            last_error = f"HTTP {response.status_code}: {detail}"
            if response.status_code == 404:
                break
            continue

        payload = response.json() if response.content else {}
        items = _extract_items(payload)
        for row in items:
            if not isinstance(row, dict):
                continue
            row = _normalize_row_keys(row)
            if not row.get("variable_present"):
                row["variable_present"] = variable_present
            name = (
                row.get("element_name")
                or row.get("whatsapp_template_name")
                or row.get("template_name")
                or row.get("name")
                or ""
            )
            lang = str(row.get("language_code") or row.get("language") or "en").strip() or "en"
            key = (str(name).strip(), lang)
            if not name or key in seen:
                continue
            seen.add(key)
            merged.append(row)

    if merged:
        return merged
    if last_error:
        raise frappe.ValidationError(last_error)
    return []


def _build_template_list_urls(account) -> List[str]:
    urls: List[str] = [ORGANIZATION_TRACK_TEMPLATES_URL]

    custom_url = (getattr(account, "interakt_templates_api_url", None) or "").strip()
    if custom_url:
        urls.insert(0, custom_url.rstrip("/"))

    org_id = (getattr(account, "interakt_organization_id", None) or "").strip()
    if not org_id and custom_url:
        match = ORG_ID_PATTERN.search(custom_url)
        if match:
            org_id = match.group(1)

    if org_id:
        from wa_chat_hub.interakt.account_config import interakt_api_root

        base = f"{interakt_api_root(account)}/organizations/{org_id}"
        for path in (
            f"{V2_TEMPLATE_PATH}/",
            "message-templates/",
            "whatsapp-message-templates/",
            f"public/{V2_TEMPLATE_PATH}/",
        ):
            candidate = f"{base}/{path}"
            if candidate not in urls:
                urls.append(_normalize_url(candidate))

    for legacy in LEGACY_TEMPLATE_ENDPOINTS:
        if legacy not in urls:
            urls.append(legacy)

    return urls


def _normalize_url(url: str) -> str:
    url = url.strip()
    if not url.endswith("/"):
        url += "/"
    return url


def _request_templates(url: str, api_key: str) -> List[Dict[str, Any]]:
    headers = {
        "Authorization": f"Basic {api_key}",
        "Content-Type": "application/json",
    }
    params_variants = [
        {"limit": 500, "offset": 0, "approval_status": "APPROVED"},
        {"limit": 500, "offset": 0, "status": "approved"},
        {"limit": 500, "offset": 0},
        None,
    ]

    last_response = None
    saw_404 = False
    for params in params_variants:
        response = requests.get(url, headers=headers, params=params, timeout=25)
        last_response = response
        if response.status_code == 404:
            saw_404 = True
            break
        if response.status_code == 405:
            response = requests.post(url, headers=headers, json=params or {}, timeout=25)
            last_response = response
        if not response.ok:
            continue
        payload = response.json() if response.content else {}
        items = _extract_items(payload)
        if items:
            return items

    if last_response is not None:
        detail = (last_response.text or "")[:500]
        if saw_404:
            raise frappe.ValidationError(f"HTTP 404: {detail or 'Not Found'}")
        raise frappe.ValidationError(
            f"HTTP {last_response.status_code}: {detail}"
        )
    raise frappe.ValidationError("No response from Interakt templates API")


def _extract_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [_normalize_row_keys(row) for row in payload if isinstance(row, dict)]

    if not isinstance(payload, dict):
        return []

    payload = _normalize_row_keys(payload)

    for key in (
        "results",
        "data",
        "templates",
        "message_templates",
        "messageTemplates",
        "items",
        "records",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            return [_normalize_row_keys(row) for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            value = _normalize_row_keys(value)
            for inner_key in (
                "results",
                "templates",
                "message_templates",
                "messageTemplates",
                "items",
                "data",
                "records",
            ):
                inner = value.get(inner_key)
                if isinstance(inner, list):
                    return [_normalize_row_keys(row) for row in inner if isinstance(row, dict)]
    return []


def _normalize_row_keys(row: Dict[str, Any]) -> Dict[str, Any]:
    """Flatten camelCase Interakt v2 keys to snake_case-friendly access."""
    if not isinstance(row, dict):
        return row
    out = dict(row)
    mapping = {
        "elementName": "element_name",
        "templateName": "template_name",
        "displayName": "display_name",
        "languageCode": "language_code",
        "approvalStatus": "approval_status",
        "templateStatus": "template_status",
        "templateCategory": "template_category",
        "whatsappTemplateName": "whatsapp_template_name",
        "bodyText": "body_text",
        "headerText": "header_text",
        "variablePresent": "variable_present",
        "bodyVariableCount": "body_variable_count",
        "headerVariableCount": "header_variable_count",
    }
    for src, dest in mapping.items():
        if src in row and dest not in out:
            out[dest] = row[src]
    return out


def _normalize_templates(raw_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    templates: List[Dict[str, Any]] = []
    seen: set[Tuple[str, str]] = set()

    for row in raw_items:
        row = _normalize_row_keys(row)
        name = (
            row.get("element_name")
            or row.get("whatsapp_template_name")
            or row.get("template_name")
            or row.get("name")
            or row.get("code_name")
        )
        if not name:
            continue
        name = str(name).strip()

        status = str(
            row.get("approval_status")
            or row.get("status")
            or row.get("template_status")
            or row.get("state")
            or "approved"
        ).strip()
        status_lower = status.lower()
        if status_lower in {"rejected", "pending", "disabled", "deleted", "draft", "paused"}:
            continue
        if status_lower and status_lower not in APPROVED_STATUSES and "approv" not in status_lower:
            continue

        display_name = (
            row.get("display_name")
            or row.get("label")
            or row.get("friendly_name")
            or row.get("title")
            or row.get("template_title")
        )
        if not display_name or str(display_name).strip() == name:
            display_name = name.replace("_", " ").title()

        languages = _extract_languages(row)
        language_code = languages[0] if languages else "en"
        dedupe_key = (name, language_code)
        if dedupe_key in seen:
            continue

        body_preview = _extract_body_preview(row)
        header_preview = _extract_header_preview(row)
        body_vars, header_vars = _count_variables(row, body_preview, header_preview)
        variable_present = str(row.get("variable_present") or "").strip()
        has_variables = _row_has_variables(row, body_vars, header_vars, variable_present)
        body_variables = _extract_variable_slots(body_preview, body_vars, "body")
        header_variables = _extract_variable_slots(header_preview, header_vars, "header")

        templates.append(
            {
                "name": name,
                "display_name": str(display_name).strip(),
                "language_code": language_code,
                "languages": languages,
                "category": row.get("category") or row.get("template_category") or "",
                "status": status,
                "body_preview": body_preview,
                "header_preview": header_preview,
                "body_variable_count": body_vars,
                "header_variable_count": header_vars,
                "body_variables": body_variables,
                "header_variables": header_variables,
                "variable_present": variable_present,
                "has_variables": has_variables,
                "source": "api",
            }
        )
        seen.add(dedupe_key)

    templates.sort(key=lambda item: (item.get("display_name") or item.get("name") or "").lower())
    return templates


def _extract_languages(row: Dict[str, Any]) -> List[str]:
    langs: List[str] = []
    for key in ("language_code", "language", "lang"):
        value = row.get(key)
        if value:
            langs.append(str(value).strip())

    for key in ("languages", "language_codes"):
        value = row.get(key)
        if isinstance(value, list):
            langs.extend(str(v).strip() for v in value if v)
        elif isinstance(value, str) and value.strip():
            langs.extend(part.strip() for part in value.split(",") if part.strip())

    components = row.get("components")
    if isinstance(components, list):
        for comp in components:
            if isinstance(comp, dict) and comp.get("language"):
                langs.append(str(comp["language"]).strip())

    deduped = []
    for lang in langs:
        if lang and lang not in deduped:
            deduped.append(lang)
    return deduped or ["en"]


def _extract_body_preview(row: Dict[str, Any]) -> str:
    for key in ("body", "body_text", "message", "content", "template_body"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    components = row.get("components")
    if isinstance(components, list):
        for comp in components:
            if not isinstance(comp, dict):
                continue
            comp = _normalize_row_keys(comp)
            ctype = str(comp.get("type") or "").upper()
            text = comp.get("text") or comp.get("body") or comp.get("content")
            if ctype == "BODY" and text:
                return str(text).strip()
    return ""


def _extract_header_preview(row: Dict[str, Any]) -> str:
    for key in ("header", "header_text"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    components = row.get("components")
    if isinstance(components, list):
        for comp in components:
            if not isinstance(comp, dict):
                continue
            comp = _normalize_row_keys(comp)
            if str(comp.get("type") or "").upper() != "HEADER":
                continue
            text = comp.get("text") or comp.get("body") or comp.get("content")
            if text:
                return str(text).strip()
    return ""


def _extract_variable_slots(
    text: str, expected_count: int = 0, section: str = "body"
) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    indices: List[int] = []
    for match in re.finditer(r"\{\{(\d+)\}\}", text):
        idx = int(match.group(1))
        if idx not in indices:
            indices.append(idx)
    if not indices and expected_count > 0:
        indices = list(range(1, expected_count + 1))
    slots: List[Dict[str, Any]] = []
    for idx in indices:
        placeholder = f"{{{{{idx}}}}}"
        slots.append(
            {
                "index": idx,
                "placeholder": placeholder,
                "context": _variable_context_snippet(text, placeholder),
                "section": section,
            }
        )
    return slots


def _variable_context_snippet(text: str, placeholder: str, radius: int = 20) -> str:
    if not text:
        return placeholder
    pos = text.find(placeholder)
    if pos < 0:
        return placeholder
    start = max(0, pos - radius)
    end = min(len(text), pos + len(placeholder) + radius)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _row_has_variables(
    row: Dict[str, Any],
    body_vars: int,
    header_vars: int,
    variable_present: str,
) -> bool:
    if body_vars or header_vars:
        return True
    vp = variable_present.lower()
    if vp in {"yes", "true", "1"}:
        return True
    for key in ("has_variables", "has_variable", "variables_present"):
        value = row.get(key)
        if value in (True, 1, "1", "yes", "Yes", "YES", "true"):
            return True
    return False


def _count_variables(
    row: Dict[str, Any], body_preview: str, header_preview: str = ""
) -> tuple[int, int]:
    body_count = int(
        row.get("body_variable_count")
        or row.get("body_variables_count")
        or row.get("number_of_body_variables")
        or row.get("body_variable")
        or 0
    )
    header_count = int(
        row.get("header_variable_count")
        or row.get("header_variables_count")
        or row.get("number_of_header_variables")
        or row.get("header_variable")
        or 0
    )
    if body_count or header_count:
        return body_count, header_count

    variable_present = str(row.get("variable_present") or "").strip().lower()
    if variable_present == "no":
        return 0, header_count

    text = body_preview or ""
    body_placeholders = re.findall(r"\{\{[^}]+\}\}|\{\d+\}", text)
    body_count = len(body_placeholders)

    header_text = header_preview or ""
    header_placeholders = re.findall(r"\{\{[^}]+\}\}|\{\d+\}", header_text)
    header_count = max(header_count, len(header_placeholders))

    if variable_present == "yes" and not body_count and not header_count:
        body_count = 1

    return body_count, header_count


def resolve_channel_account_from_conversation(conversation: str) -> str:
    channel_account = safe_ai_get_value("Chat Conversation", conversation, "channel_account")
    if not channel_account:
        frappe.throw(_("Conversation has no channel account"))
    return channel_account
