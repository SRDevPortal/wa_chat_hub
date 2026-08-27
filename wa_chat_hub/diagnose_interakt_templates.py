"""Probe Interakt template list endpoints for a channel account."""

from __future__ import annotations

import frappe

from wa_chat_hub.interakt.templates_api import (
    ORGANIZATION_TRACK_TEMPLATES_URL,
    _build_template_list_urls,
    _fetch_organization_track_templates,
    _load_manual_catalog,
    _normalize_templates,
    _request_templates,
)


def run(channel_account: str = "Interakt Main"):
    account = frappe.get_doc("Chat Channel Account", channel_account)
    api_key = account.get_password("interakt_api_key")
    print(f"Channel account: {channel_account}")
    print(f"Organization ID: {getattr(account, 'interakt_organization_id', '') or '(empty)'}")
    print(f"Manual catalog rows: {len(_load_manual_catalog(account))}")
    if not api_key:
        print("No API key configured.")
        return

    try:
        items = _fetch_organization_track_templates(api_key)
        normalized = _normalize_templates(items)
        with_vars = sum(1 for row in normalized if row.get("has_variables"))
        print(
            f"OK {ORGANIZATION_TRACK_TEMPLATES_URL} -> "
            f"{len(items)} raw, {len(normalized)} approved, {with_vars} with variables"
        )
    except Exception as exc:
        print(f"FAIL {ORGANIZATION_TRACK_TEMPLATES_URL} -> {exc}")

    for url in _build_template_list_urls(account):
        if url.rstrip("/") == ORGANIZATION_TRACK_TEMPLATES_URL:
            continue
        try:
            items = _request_templates(url, api_key)
            print(f"OK {url} -> {len(items)} raw item(s)")
        except Exception as exc:
            print(f"FAIL {url} -> {exc}")
