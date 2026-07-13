"""Meta / Interakt messaging window rules for Chat Conversation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime
from pymysql.err import InterfaceError, OperationalError

from wa_chat_hub.messaging.attribution import extract_attribution, json_loads_payload
from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_set_value,
)

CUSTOMER_SERVICE_HOURS = 24
CTWA_ENTRY_HOURS = 72
BACKFILL_COMPLETED_DEFAULT = "wa_chat_hub_messaging_windows_backfilled"
TEMPLATE_CONTENT_TYPES = frozenset({"template"})
_WINDOW_FIELD_NAMES = (
    "messaging_window_mode",
    "last_customer_message_at",
    "customer_service_window_expires_at",
    "ctwa_entry_at",
    "ctwa_window_expires_at",
    "last_template_sent_at",
    "last_template_category",
    "source_id",
    "source_url",
    "source",
    "ctwa_clid",
)
DB_CONNECTION_ERROR_CODES = {2006, 2013}


def _is_db_connection_error(exc: Exception) -> bool:
    if isinstance(exc, InterfaceError):
        return True
    if isinstance(exc, OperationalError):
        return bool(exc.args and exc.args[0] in DB_CONNECTION_ERROR_CODES)
    return False


def _recover_db_connection() -> None:
    try:
        frappe.db.close()
    except Exception:
        pass
    frappe.db.connect()


def _safe_log_error(title: str) -> None:
    traceback = frappe.get_traceback()
    try:
        frappe.log_error(traceback, title)
    except Exception:
        frappe.logger("wa_chat_hub").error("%s\n%s", title, traceback, exc_info=True)
        if frappe.db:
            _recover_db_connection()


def messaging_windows_backfill_completed() -> bool:
    """Return true when historical conversations do not need the backfill anymore."""
    if frappe.db.get_global(BACKFILL_COMPLETED_DEFAULT) == "1":
        return True
    if not safe_ai_exists("DocType", "Chat Conversation"):
        return True
    if not safe_ai_exists("DocType", "Chat Message"):
        return True

    meta = frappe.get_meta("Chat Conversation")
    if not meta.has_field("last_customer_message_at") or not meta.has_field("messaging_window_mode"):
        return False

    pending_conditions = [
        "IFNULL(c.messaging_window_mode, '') = ''",
        """
        (
            c.last_customer_message_at IS NULL
            AND EXISTS (
                SELECT 1
                FROM `tabChat Message` m
                WHERE m.conversation = c.name
                  AND m.direction = 'Inbound'
                  AND COALESCE(NULLIF(m.sender_type, ''), 'Customer') NOT IN ('Agent', 'AI', 'System')
                LIMIT 1
            )
        )
        """,
    ]
    if (
        meta.has_field("ctwa_clid")
        and meta.has_field("ctwa_entry_at")
        and meta.has_field("ctwa_window_expires_at")
    ):
        pending_conditions.append(
            """
            (
                IFNULL(c.ctwa_clid, '') = ''
                AND (c.ctwa_entry_at IS NOT NULL OR c.ctwa_window_expires_at IS NOT NULL)
            )
            """
        )

    assert_ai_doctype_permission("Chat Conversation", "read")
    assert_ai_doctype_permission("Chat Message", "read")
    rows = frappe.db.sql(
        f"""
        SELECT c.name
        FROM `tabChat Conversation` c
        WHERE {" OR ".join(pending_conditions)}
        LIMIT 1
        """,
        as_dict=True,
    )
    completed = not rows
    if completed:
        mark_messaging_windows_backfill_completed()
    return completed


def mark_messaging_windows_backfill_completed() -> None:
    frappe.db.set_global(BACKFILL_COMPLETED_DEFAULT, "1")


def _messaging_window_fields_ready() -> bool:
    if not safe_ai_exists("DocType", "Chat Conversation"):
        return False
    meta = frappe.get_meta("Chat Conversation")
    return meta.has_field("customer_service_window_expires_at")


def _ensure_messaging_window_schema() -> None:
    """Reload DocType after code deploy so new columns are visible to the ORM."""
    if _messaging_window_fields_ready():
        return
    try:
        frappe.reload_doc("wa_chat_hub", "doctype", "Chat Conversation", force=True)
        frappe.clear_cache(doctype="Chat Conversation")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Chat Conversation DocType Reload Failed")


def _convo_field(convo: Any, fieldname: str, default=None):
    return getattr(convo, fieldname, default)


def _set_convo_field(convo: Any, fieldname: str, value: Any) -> None:
    if _messaging_window_fields_ready():
        setattr(convo, fieldname, value)


def _last_customer_inbound_at(conversation: str):
    """Latest inbound message from the customer (not agent/AI/system)."""
    assert_ai_doctype_permission("Chat Message", "read")
    rows = frappe.db.sql(
        """
        SELECT MAX(creation) AS last_at
        FROM `tabChat Message`
        WHERE conversation = %s
          AND direction = 'Inbound'
          AND COALESCE(NULLIF(sender_type, ''), 'Customer') NOT IN ('Agent', 'AI', 'System')
        """,
        (conversation,),
        as_dict=True,
    )
    if not rows or not rows[0].last_at:
        return None
    return get_datetime(rows[0].last_at)


def _build_window_state_payload(
    *,
    now,
    last_customer_at,
    cs_expires,
    cs_active: bool,
    ctwa_expires,
    ctwa_active: bool,
    convo: Any = None,
    schema_pending: bool = False,
) -> Dict[str, Any]:
    can_send_free_form = cs_active or ctwa_active
    free_form_expires_at = None
    if cs_active and ctwa_active:
        free_form_expires_at = max(cs_expires, ctwa_expires)
    elif cs_active:
        free_form_expires_at = cs_expires
    elif ctwa_active:
        free_form_expires_at = ctwa_expires

    if can_send_free_form:
        mode = "free_form"
        if cs_active and not ctwa_active:
            reason = "customer_replied_within_24h"
        elif ctwa_active and not cs_active:
            reason = "ctwa_72h"
        else:
            reason = "customer_service_or_ctwa_active"
    else:
        mode = "template_only"
        reason = "window_closed_use_template"

    payload = {
        "mode": mode,
        "reason": reason,
        "can_send_free_form": can_send_free_form,
        "can_send_template": True,
        "customer_service_active": cs_active,
        "ctwa_active": ctwa_active,
        "free_form_expires_at": str(free_form_expires_at) if free_form_expires_at else None,
        "customer_service_expires_at": str(cs_expires) if cs_expires else None,
        "ctwa_expires_at": str(ctwa_expires) if ctwa_expires else None,
        "last_customer_message_at": str(last_customer_at) if last_customer_at else None,
        "server_time": str(now),
    }
    if convo is not None:
        payload.update(
            {
                "ctwa_entry_at": str(_convo_field(convo, "ctwa_entry_at") or "") or None,
                "last_template_sent_at": str(_convo_field(convo, "last_template_sent_at") or "") or None,
                "last_template_category": _convo_field(convo, "last_template_category") or None,
                "source_id": _convo_field(convo, "source_id") or None,
                "source_url": _convo_field(convo, "source_url") or None,
                "source": _convo_field(convo, "source") or None,
                "ctwa_clid": _convo_field(convo, "ctwa_clid") or None,
            }
        )
    if schema_pending:
        payload["schema_pending"] = True
    return payload


def _fallback_window_state_from_messages(conversation: str, now) -> Dict[str, Any]:
    """Compute window from Chat Message history when DB columns are not migrated yet."""
    last_at = _last_customer_inbound_at(conversation)
    cs_expires = (
        add_to_date(last_at, hours=CUSTOMER_SERVICE_HOURS, as_datetime=True) if last_at else None
    )
    cs_active = bool(cs_expires and now < cs_expires)
    return _build_window_state_payload(
        now=now,
        last_customer_at=last_at,
        cs_expires=cs_expires,
        cs_active=cs_active,
        ctwa_expires=None,
        ctwa_active=False,
        schema_pending=True,
    )


@dataclass
class WindowDecision:
    allowed: bool
    mode: str
    reason: str
    can_send_free_form: bool
    can_send_template: bool = True

    def ensure_allowed(self, content_type: str) -> None:
        if self.allowed:
            return
        frappe.throw(
            _(
                "Free-form WhatsApp messages are not allowed outside the messaging window. "
                "Use an approved Template to re-engage. ({0})"
            ).format(self.reason),
            title=_("Messaging Window Closed"),
        )


def update_windows_on_message(
    conversation: str,
    *,
    direction: str,
    sender_type: str = "Customer",
    content_type: str = "Text",
    raw_payload: Any = None,
    message_time: Optional[str] = None,
    template_category: Optional[str] = None,
) -> Dict[str, Any]:
    """Update conversation window fields after a message is stored."""
    _ensure_messaging_window_schema()
    if not _messaging_window_fields_ready():
        return get_messaging_window_state(conversation, now=message_time)

    convo = safe_ai_get_doc("Chat Conversation", conversation)
    now = get_datetime(message_time) if message_time else now_datetime()
    direction = (direction or "Inbound").strip()
    sender_type = (sender_type or "Customer").strip()
    content_type = str(content_type or "Text").strip()
    payload = _coerce_payload(raw_payload)

    is_customer_inbound = direction == "Inbound" and sender_type == "Customer"
    is_template_outbound = direction == "Outbound" and content_type.lower() in TEMPLATE_CONTENT_TYPES
    db_updates: Dict[str, Any] = {}

    if is_customer_inbound:
        cs_expires = add_to_date(now, hours=CUSTOMER_SERVICE_HOURS, as_datetime=True)
        _set_convo_field(convo, "last_customer_message_at", now)
        _set_convo_field(convo, "customer_service_window_expires_at", cs_expires)
        db_updates["last_customer_message_at"] = now
        db_updates["customer_service_window_expires_at"] = cs_expires
        _apply_attribution(convo, payload, entry_time=now)
        for field in (
            "source_id",
            "source_url",
            "source",
            "ctwa_clid",
            "ctwa_entry_at",
            "ctwa_window_expires_at",
        ):
            value = _convo_field(convo, field)
            if value:
                db_updates[field] = value

    if is_template_outbound:
        _set_convo_field(convo, "last_template_sent_at", now)
        db_updates["last_template_sent_at"] = now
        if template_category:
            _set_convo_field(convo, "last_template_category", template_category)
            db_updates["last_template_category"] = template_category

    mode = _compute_mode(convo, now)
    _set_convo_field(convo, "messaging_window_mode", mode)
    db_updates["messaging_window_mode"] = mode

    if db_updates:
        safe_ai_set_value(
            "Chat Conversation",
            conversation,
            db_updates,
            update_modified=False,
        )

    if is_customer_inbound:
        try:
            from wa_chat_hub.messaging.crm_lead_meta import sync_crm_lead_meta_from_conversation

            sync_crm_lead_meta_from_conversation(convo, raw_payload=payload)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "CRM Lead Meta Sync Failed")

    state = get_messaging_window_state(conversation, now=now, convo=convo)
    frappe.publish_realtime(
        "wa_chat_window_updated",
        {"conversation": conversation, "messaging_window": state},
        after_commit=True,
    )
    return state


def evaluate_send_permission(conversation: str, content_type: str) -> WindowDecision:
    """Return whether an outbound message type may be sent now."""
    normalized = str(content_type or "Text").strip().lower()
    if normalized in TEMPLATE_CONTENT_TYPES:
        return WindowDecision(
            allowed=True,
            mode="template_only",
            reason="template_always_allowed",
            can_send_free_form=False,
            can_send_template=True,
        )

    state = get_messaging_window_state(conversation)
    if state.get("can_send_free_form"):
        return WindowDecision(
            allowed=True,
            mode=state.get("mode") or "free_form",
            reason=state.get("reason") or "free_form_active",
            can_send_free_form=True,
            can_send_template=True,
        )

    return WindowDecision(
        allowed=False,
        mode=state.get("mode") or "template_only",
        reason=state.get("reason") or "window_closed_use_template",
        can_send_free_form=False,
        can_send_template=bool(state.get("can_send_template", True)),
    )


def get_messaging_window_state(
    conversation: str,
    *,
    now: Optional[Any] = None,
    convo: Optional[Any] = None,
) -> Dict[str, Any]:
    """Serializable window state for API and UI."""
    _ensure_messaging_window_schema()
    now = get_datetime(now) if now else now_datetime()
    if not _messaging_window_fields_ready():
        return _fallback_window_state_from_messages(conversation, now)

    if convo is None:
        convo = safe_ai_get_doc("Chat Conversation", conversation)

    # Runtime window checks use the state maintained when messages are accepted.
    # Scanning Chat Message history is reserved for the explicit repair/backfill path.
    last_raw = _convo_field(convo, "last_customer_message_at")
    last_at = get_datetime(last_raw) if last_raw else None
    cs_expires = (
        add_to_date(last_at, hours=CUSTOMER_SERVICE_HOURS, as_datetime=True) if last_at else None
    )
    cs_active = bool(cs_expires and now < cs_expires)

    # 72h CTWA: only when ctwa_clid is stored (strict); ignore false-positive backfill.
    ctwa_expires = None
    ctwa_active = False
    if (_convo_field(convo, "ctwa_clid") or "").strip():
        ctwa_raw = _convo_field(convo, "ctwa_window_expires_at")
        if ctwa_raw:
            ctwa_expires = get_datetime(ctwa_raw)
            ctwa_active = bool(ctwa_expires and now < ctwa_expires)

    return _build_window_state_payload(
        now=now,
        last_customer_at=last_at,
        cs_expires=cs_expires,
        cs_active=cs_active,
        ctwa_expires=ctwa_expires,
        ctwa_active=ctwa_active,
        convo=convo,
    )


def detect_ctwa_entry(raw_payload: Any) -> bool:
    """True only for Click-to-WhatsApp ad entry — not generic WhatsApp source labels."""
    payload = _coerce_payload(raw_payload)
    attribution = extract_attribution(payload)
    if (attribution.get("ctwa_clid") or "").strip():
        return True
    if _find_referral_dict(payload):
        return True
    source = (attribution.get("source") or "").lower().replace(" ", "_")
    if "ctwa" in source or "click_to_whatsapp" in source:
        return True
    return False


def repair_ctwa_false_positives() -> None:
    """Clear CTWA window rows that were set without a real ctwa_clid (bad backfill)."""
    if not _messaging_window_fields_ready():
        return
    assert_ai_doctype_permission("Chat Conversation", "write")
    frappe.db.sql(
        """
        UPDATE `tabChat Conversation`
        SET ctwa_entry_at = NULL,
            ctwa_window_expires_at = NULL
        WHERE IFNULL(ctwa_clid, '') = ''
          AND (ctwa_entry_at IS NOT NULL OR ctwa_window_expires_at IS NOT NULL)
        """
    )


@frappe.whitelist()
def repair_messaging_windows():
    """Recompute window fields for all conversations (admin maintenance)."""
    repair_ctwa_false_positives()
    backfill_messaging_windows_from_history(force=True)
    return {"success": True, "message": "Messaging windows repaired"}


def backfill_messaging_windows_from_history(force: bool = False) -> None:
    """Set window fields from existing Chat Message rows."""
    if not force and messaging_windows_backfill_completed():
        return
    if not safe_ai_exists("DocType", "Chat Conversation"):
        return
    meta = frappe.get_meta("Chat Conversation")
    if not meta.has_field("last_customer_message_at"):
        return

    repair_ctwa_false_positives()

    conversations = safe_ai_get_all("Chat Conversation", pluck="name")
    for conversation in conversations:
        try:
            _backfill_single_conversation(conversation)
        except Exception as exc:
            _safe_log_error(f"Messaging Window Backfill Failed: {conversation}")
            if _is_db_connection_error(exc):
                raise

    mark_messaging_windows_backfill_completed()


def _backfill_single_conversation(conversation: str) -> None:
    now = now_datetime()
    last_at = _last_customer_inbound_at(conversation)
    if not last_at:
        safe_ai_set_value(
            "Chat Conversation",
            conversation,
            {
                "messaging_window_mode": "template_only",
                "last_customer_message_at": None,
                "customer_service_window_expires_at": None,
                "ctwa_entry_at": None,
                "ctwa_window_expires_at": None,
            },
            update_modified=False,
        )
        return

    cs_expires = add_to_date(last_at, hours=CUSTOMER_SERVICE_HOURS, as_datetime=True)
    updates: Dict[str, Any] = {
        "last_customer_message_at": last_at,
        "customer_service_window_expires_at": cs_expires,
        "ctwa_entry_at": None,
        "ctwa_window_expires_at": None,
    }

    inbound_rows = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation, "direction": "Inbound"},
        fields=["raw_payload", "creation"],
        order_by="creation asc",
        limit_page_length=100,
    )
    for row in inbound_rows:
        payload = json_loads_payload(row.raw_payload)
        if not detect_ctwa_entry(payload):
            continue
        attribution = extract_attribution(payload)
        entry_at = get_datetime(row.creation)
        updates["ctwa_entry_at"] = entry_at
        updates["ctwa_window_expires_at"] = add_to_date(
            entry_at, hours=CTWA_ENTRY_HOURS, as_datetime=True
        )
        for key in ("source_id", "source_url", "source", "ctwa_clid"):
            if attribution.get(key):
                updates[key] = attribution[key]
        break

    cs_active = bool(cs_expires and now < cs_expires)
    ctwa_expires_dt = (
        get_datetime(updates["ctwa_window_expires_at"])
        if updates.get("ctwa_window_expires_at")
        else None
    )
    ctwa_active = bool(ctwa_expires_dt and now < ctwa_expires_dt and updates.get("ctwa_clid"))
    if not updates.get("ctwa_clid"):
        updates["ctwa_entry_at"] = None
        updates["ctwa_window_expires_at"] = None
        ctwa_active = False

    updates["messaging_window_mode"] = "free_form" if (cs_active or ctwa_active) else "template_only"
    safe_ai_set_value("Chat Conversation", conversation, updates, update_modified=False)


def _compute_mode(convo, now) -> str:
    cs_raw = _convo_field(convo, "customer_service_window_expires_at")
    ctwa_raw = _convo_field(convo, "ctwa_window_expires_at")
    cs_expires = get_datetime(cs_raw) if cs_raw else None
    ctwa_expires = get_datetime(ctwa_raw) if ctwa_raw else None
    if (cs_expires and now < cs_expires) or (ctwa_expires and now < ctwa_expires):
        return "free_form"
    return "template_only"


def _apply_attribution(convo, payload: Dict[str, Any], entry_time) -> None:
    attribution = extract_attribution(payload)
    for field in ("source_id", "source_url", "source", "ctwa_clid"):
        value = attribution.get(field)
        if value and not _convo_field(convo, field):
            _set_convo_field(convo, field, value)

    if detect_ctwa_entry(payload) and not _convo_field(convo, "ctwa_entry_at"):
        _set_convo_field(convo, "ctwa_entry_at", entry_time)
        _set_convo_field(
            convo,
            "ctwa_window_expires_at",
            add_to_date(entry_time, hours=CTWA_ENTRY_HOURS, as_datetime=True),
        )


def _coerce_payload(raw_payload: Any) -> Dict[str, Any]:
    if isinstance(raw_payload, dict):
        return raw_payload
    if isinstance(raw_payload, str) and raw_payload.strip():
        try:
            parsed = json.loads(raw_payload)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return json_loads_payload(raw_payload)


def _find_referral_dict(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for key in ("referral", "click_to_whatsapp", "context"):
        value = payload.get(key)
        if isinstance(value, dict) and value:
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("referral", "message", "customer"):
            nested = data.get(key)
            if isinstance(nested, dict):
                found = _find_referral_dict(nested)
                if found:
                    return found
    return None
