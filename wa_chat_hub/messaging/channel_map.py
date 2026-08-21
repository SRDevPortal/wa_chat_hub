"""Resolve Interakt Chat Channel Account from WA Channel Pipeline Map."""

from __future__ import annotations

from typing import Any, Dict, Optional

import frappe
from frappe import _

from wa_chat_hub.security import safe_ai_exists, safe_ai_get_all, safe_ai_get_doc


PIPELINE_MAP_FIELDS = [
    "name",
    "chat_channel_account",
    "sr_lead_pipeline",
    "sr_lead_source",
    "is_active",
]


def get_pipeline_map(
    *,
    pipeline: Optional[str] = None,
    channel_account: Optional[str] = None,
) -> Dict[str, Any]:
    """Return a single active WA Channel Pipeline Map row."""
    if not safe_ai_exists("DocType", "WA Channel Pipeline Map"):
        frappe.throw(_("WA Channel Pipeline Map is not installed."))

    filters: Dict[str, Any] = {"is_active": 1}
    if channel_account:
        filters["chat_channel_account"] = channel_account
    if pipeline:
        filters["sr_lead_pipeline"] = pipeline

    rows = safe_ai_get_all(
        "WA Channel Pipeline Map",
        filters=filters,
        fields=_pipeline_map_fields(),
        limit_page_length=2,
    )
    if not rows:
        hint = _missing_map_hint(pipeline=pipeline)
        frappe.throw(hint)
    if len(rows) > 1:
        frappe.throw(_("Multiple active WA Channel Pipeline Map records match. Use one row per Interakt account."))

    row = rows[0]
    _validate_channel_account(row["chat_channel_account"])
    return row


def get_channel_account_for_lead(lead) -> str:
    pipeline = lead.get("sr_lead_pipeline")
    if not pipeline:
        frappe.throw(_("CRM Lead {0} does not have a pipeline (sr_lead_pipeline).").format(lead.name))
    return get_pipeline_map(pipeline=pipeline)["chat_channel_account"]


def get_pipeline_map_row_for_channel_account(channel_account: Optional[str]) -> Optional[Dict[str, Any]]:
    """One active WA Channel Pipeline Map row per Interakt Chat Channel Account."""
    if not channel_account or not safe_ai_exists("DocType", "WA Channel Pipeline Map"):
        return None
    rows = safe_ai_get_all(
        "WA Channel Pipeline Map",
        filters={"chat_channel_account": channel_account, "is_active": 1},
        fields=_pipeline_map_fields(),
        limit_page_length=1,
    )
    return rows[0] if rows else None


def get_pipeline_for_channel_account(channel_account: Optional[str]) -> Optional[str]:
    """
    Default SR Lead Pipeline for this Interakt account (not per WhatsApp user).
    Returns None if WA Channel Pipeline Map is missing — callers must handle without breaking inbound chat.
    """
    row = get_pipeline_map_row_for_channel_account(channel_account)
    return row.get("sr_lead_pipeline") if row else None


def get_source_for_channel_account(channel_account: Optional[str]) -> Optional[str]:
    """Optional SR Lead Source for CRM Leads created from this Interakt account."""
    row = get_pipeline_map_row_for_channel_account(channel_account)
    return row.get("sr_lead_source") if row else None


def get_channel_account_defaults(channel_account: Optional[str]) -> Dict[str, Any]:
    """
    Defaults from WA Channel Pipeline Map for one Interakt Chat Channel Account.
    """
    row = get_pipeline_map_row_for_channel_account(channel_account)
    if not row:
        return {}
    return {
        "sr_lead_pipeline": row.get("sr_lead_pipeline"),
        "sr_lead_source": row.get("sr_lead_source"),
        "pipeline_map": row.get("name"),
    }


def require_sr_lead_pipeline_for_channel_account(channel_account: str) -> str:
    """Mandatory default pipeline for new CRM Leads on this Interakt account."""
    pipeline = get_pipeline_for_channel_account(channel_account)
    if pipeline:
        return pipeline
    frappe.throw(
        _(
            "WA Channel Pipeline Map is missing for Chat Channel Account {0}. "
            "Create exactly one active row linking this Interakt account to its default SR Lead Pipeline."
        ).format(channel_account)
    )


def _validate_channel_account(channel_account: str) -> None:
    account = safe_ai_get_doc("Chat Channel Account", channel_account)
    if not account.is_active:
        frappe.throw(_("Mapped WhatsApp channel {0} is not active.").format(channel_account))
    if account.channel_type != "Interakt":
        frappe.throw(_("Mapped WhatsApp channel {0} must be an Interakt account.").format(channel_account))


def _pipeline_map_fields() -> list[str]:
    fields = list(PIPELINE_MAP_FIELDS)
    try:
        meta = frappe.get_meta("WA Channel Pipeline Map")
    except Exception:
        return [field for field in fields if field != "sr_lead_source"]
    return [field for field in fields if field != "sr_lead_source" or meta.has_field(field)]


def _missing_map_hint(*, pipeline: Optional[str]) -> str:
    if pipeline:
        return _("No active WA Channel Pipeline Map for SR Lead Pipeline {0}.").format(pipeline)
    return _("No active WA Channel Pipeline Map found.")
