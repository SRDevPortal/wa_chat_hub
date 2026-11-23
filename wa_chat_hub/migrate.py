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
    """Refresh the workspace after schema synchronization.

    Historical backfills are manual-only; migration never runs or queues them.
    """
    from wa_chat_hub.security import ensure_default_ai_doctype_permissions

    ensure_default_ai_doctype_permissions()
    try:
        from wa_chat_hub.setup_workspace import run as setup_workspace

        setup_workspace()
    except Exception:
        _safe_log_error("WA Chat Hub Workspace Sync Failed")
    ensure_app_update_setting()


def backfill_indexed_phone_keys() -> dict:
    """Historical normalization is explicitly operated through Bench, never migration."""
    return {"status": "manual_backfill_required"}


def backfill_messaging_windows() -> dict:
    """Retired enqueue entry point, including calls from old queued jobs."""
    return {"status": "manual_backfill_required"}


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
        from wa_chat_hub.patches.v1_0.add_customer_indexed_phone_lookup import (
            ensure_customer_phone_lookup_schema,
        )
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
        ensure_customer_phone_lookup_schema()
    except Exception:
        _safe_log_error("Chat Message Index Sync Failed")


def ensure_app_update_indexes() -> None:
    """Keep filtered update-log history reads index-backed."""
    table_name = "tabWA App Update Log"
    index_name = "idx_wa_update_script_executed_at"
    table_exists = frappe.db.sql(
        """
        SELECT 1
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        LIMIT 1
        """,
        table_name,
    )
    if not table_exists:
        return
    exists = frappe.db.sql(
        """
        SELECT COUNT(*)
        FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND INDEX_NAME = %s
        """,
        (table_name, index_name),
    )[0][0]
    if exists:
        return
    frappe.db.sql(
        f"""
        ALTER TABLE `{table_name}`
        ADD INDEX `{index_name}` (`update_script`, `executed_at`),
        ALGORITHM=INPLACE, LOCK=NONE
        """
    )


def ensure_app_update_setting() -> None:
    if not frappe.db.exists("DocType", "WA Chat Hub Settings"):
        return
    initialized = frappe.db.get_single_value(
        "WA Chat Hub Settings", "app_update_system_initialized"
    )
    if not initialized:
        frappe.db.set_single_value("WA Chat Hub Settings", "enable_app_update_system", 1)
        frappe.db.set_single_value("WA Chat Hub Settings", "app_update_system_initialized", 1)


def backfill_channel_account_medical_departments(names=None):
    """Copy account defaults only within an explicitly selected manual batch."""
    if names is None:
        from wa_chat_hub.maintenance.history_backfill import run
        return run("channel_departments")
    if not frappe.db.exists("DocType", "Chat Channel Account"):
        return
    if not frappe.get_meta("Chat Channel Account").has_field("default_medical_department"):
        return
    if not frappe.db.exists("DocType", "WA Channel Pipeline Map"):
        return
    statement = """
        UPDATE `tabChat Channel Account` cca
        INNER JOIN `tabWA Channel Pipeline Map` wpm
            ON wpm.chat_channel_account = cca.name
           AND wpm.is_active = 1
        SET cca.default_medical_department = wpm.sr_medical_department
        WHERE cca.name IN %(names)s AND ({condition})
    """
    frappe.db.sql(statement.format(condition="cca.default_medical_department IS NULL"), {"names": names})
    frappe.db.sql(statement.format(condition="cca.default_medical_department = ''"), {"names": names})


def migrate_conversation_crm_lead_links(names=None):
    """Copy legacy links within an explicitly selected manual batch."""
    if names is None:
        from wa_chat_hub.maintenance.history_backfill import run
        return run("crm_lead_links")
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
        WHERE c.name IN %(names)s
          AND IFNULL(c.linked_reference_doctype, '') IN ('CRM Lead', 'Lead')
          AND IFNULL(c.linked_reference_name, '') != ''
          AND IFNULL(c.linked_crm_lead, '') = ''
        """,
        {"names": names},
    )
