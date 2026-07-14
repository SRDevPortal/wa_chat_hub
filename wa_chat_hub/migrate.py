from __future__ import annotations

import frappe
from pymysql.err import InterfaceError, OperationalError
from redis.exceptions import ConnectionError as RedisConnectionError
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


DB_CONNECTION_ERROR_CODES = {2006, 2013}
MESSAGING_WINDOW_BACKFILL_JOB_ID = "wa_chat_hub_messaging_window_backfill"


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


def _background_queue_available(queue: str) -> bool:
    try:
        from frappe.utils.background_jobs import get_queue

        get_queue(queue).connection.ping()
        return True
    except RedisConnectionError:
        frappe.logger("wa_chat_hub").warning(
            "Skipping WA Chat Hub messaging window backfill enqueue because Redis is unavailable.",
            exc_info=True,
        )
        return False
    except Exception:
        _safe_log_error("WA Chat Hub Background Queue Check Failed")
        return False


def after_migrate() -> None:
    """Run WA Chat Hub data and workspace migrations after schema synchronization."""
    try:
        from wa_chat_hub.setup_workspace import run as setup_workspace

        setup_workspace()
    except Exception:
        _safe_log_error("WA Chat Hub Workspace Sync Failed")
    ensure_lead_scoring_fields()
    ensure_chat_message_indexes()
    migrate_conversation_crm_lead_links()
    try:
        from wa_chat_hub.security import ensure_default_ai_doctype_permissions
        from wa_chat_hub.setup_agents import (
            backfill_conversation_identities,
            ensure_default_agent_profiles,
        )

        ensure_default_ai_doctype_permissions()
        ensure_default_agent_profiles()
        backfill_conversation_identities()
    except Exception:
        _safe_log_error("WA Chat Hub Agent Setup Failed")
    backfill_messaging_windows()


def backfill_messaging_windows() -> None:
    try:
        from wa_chat_hub.messaging.windows import (
            backfill_messaging_windows_from_history,
            messaging_windows_backfill_completed,
        )

        if messaging_windows_backfill_completed():
            return
        if not _background_queue_available("long"):
            return

        frappe.enqueue(
            backfill_messaging_windows_from_history,
            queue="long",
            timeout=7200,
            enqueue_after_commit=True,
            job_id=MESSAGING_WINDOW_BACKFILL_JOB_ID,
            deduplicate=True,
        )
    except Exception as exc:
        _safe_log_error("Messaging Window Backfill Enqueue Failed")
        if _is_db_connection_error(exc) and frappe.db:
            _recover_db_connection()


def ensure_lead_scoring_fields() -> None:
    specs = {
        "Lead": "source",
        "CRM Lead": "status",
    }
    custom_fields = {}
    for doctype, insert_after in specs.items():
        if not frappe.db.exists("DocType", doctype):
            continue
        custom_fields[doctype] = [
            {
                "fieldname": "lead_score",
                "label": "lead_score",
                "fieldtype": "Float",
                "insert_after": insert_after,
                "in_list_view": 1,
                "in_standard_filter": 1,
                "default": "0",
                "precision": "2",
            },
            {
                "fieldname": "lead_lan",
                "label": "lead_lan",
                "fieldtype": "Data",
                "insert_after": "lead_score",
                "in_list_view": 1,
                "in_standard_filter": 1,
            },
            {
                "fieldname": "lead_temperature",
                "label": "Lead_temperature",
                "fieldtype": "Select",
                "insert_after": "lead_lan",
                "in_list_view": 1,
                "in_standard_filter": 1,
                "options": "Cold\nWarm\nHot",
                "default": "Cold",
            },
        ]
    if custom_fields:
        create_custom_fields(custom_fields, update=True)


def ensure_chat_message_indexes() -> None:
    try:
        from wa_chat_hub.patches.v1_0.add_chat_message_indexes import (
            ensure_chat_contact_channel_profile_indexes,
            ensure_chat_contact_indexes,
            ensure_chat_conversation_indexes,
            ensure_chat_message_indexes,
            ensure_crm_lead_indexes,
            ensure_reference_phone_indexes,
        )

        ensure_chat_message_indexes()
        ensure_chat_conversation_indexes()
        ensure_chat_contact_indexes()
        ensure_chat_contact_channel_profile_indexes()
        ensure_crm_lead_indexes()
        ensure_reference_phone_indexes()
    except Exception:
        _safe_log_error("Chat Message Index Sync Failed")


def migrate_conversation_crm_lead_links() -> None:
    """Copy legacy CRM Lead / Lead links into linked_crm_lead Link field."""
    if not frappe.db.exists("DocType", "Chat Conversation"):
        return
    meta = frappe.get_meta("Chat Conversation")
    if not meta.has_field("linked_crm_lead"):
        return

    if not frappe.db.exists("DocType", "CRM Lead"):
        return

    frappe.db.sql(
        """
        UPDATE `tabChat Conversation` c
        INNER JOIN `tabCRM Lead` l ON l.name = c.linked_reference_name
        SET c.linked_crm_lead = c.linked_reference_name,
            c.linked_reference_doctype = 'CRM Lead'
        WHERE IFNULL(c.linked_reference_doctype, '') IN ('CRM Lead', 'Lead')
          AND IFNULL(c.linked_reference_name, '') != ''
          AND IFNULL(c.linked_crm_lead, '') = ''
        """
    )
