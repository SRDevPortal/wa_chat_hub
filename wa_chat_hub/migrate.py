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
    """Refresh workspace and idempotent WA Chat Hub custom fields after migrate."""
    try:
        from wa_chat_hub.setup_workspace import run as setup_workspace

        setup_workspace()
    except Exception:
        _safe_log_error("WA Chat Hub Workspace Sync Failed")
    ensure_lead_scoring_fields()
    ensure_shipkia_business_type_options()
    try:
        from wa_chat_hub.setup_ai_routing import seed_default_ai_routing

        seed_default_ai_routing()
    except Exception:
        _safe_log_error("WA Chat Hub Policy Seed Upgrade Failed")
    ensure_app_update_setting()


def backfill_indexed_phone_keys() -> None:
    try:
        settings = frappe.get_cached_doc("WA Chat Hub Settings")
        if settings.meta.has_field("enable_indexed_phone_lookup") and settings.enable_indexed_phone_lookup:
            return
        if not _background_queue_available("long"):
            return
        frappe.enqueue(
            "wa_chat_hub.maintenance.phone_backfill.run_indexed_phone_backfill",
            queue="long",
            timeout=1800,
            enqueue_after_commit=True,
            job_id="wa_chat_hub_indexed_phone_backfill_0",
            deduplicate=True,
            doctype_index=0,
        )
    except Exception:
        _safe_log_error("Indexed Phone Backfill Enqueue Failed")


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
    shipkia_lead_fields = [
        {
            "fieldname": "shipkia_section",
            "label": "ShipKia WhatsApp",
            "fieldtype": "Section Break",
            "insert_after": "lead_temperature",
            "collapsible": 1,
        },
        {
            "fieldname": "shipkia_business_type",
            "label": "ShipKia Business Type",
            "fieldtype": "Data",
            "insert_after": "shipkia_section",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "shipkia_business_name",
            "label": "ShipKia Business Name",
            "fieldtype": "Data",
            "insert_after": "shipkia_business_type",
        },
        {
            "fieldname": "shipkia_monthly_shipments",
            "label": "ShipKia Monthly Shipments",
            "fieldtype": "Int",
            "insert_after": "shipkia_business_name",
            "in_list_view": 1,
            "in_standard_filter": 1,
        },
        {
            "fieldname": "shipkia_current_aggregator_status",
            "label": "ShipKia Current Aggregator Status",
            "fieldtype": "Data",
            "insert_after": "shipkia_monthly_shipments",
        },
        {
            "fieldname": "shipkia_current_aggregator_name",
            "label": "ShipKia Current Aggregator Name",
            "fieldtype": "Data",
            "insert_after": "shipkia_current_aggregator_status",
        },
        {
            "fieldname": "shipkia_current_aggregator_raw",
            "label": "ShipKia Current Aggregator Raw",
            "fieldtype": "Small Text",
            "insert_after": "shipkia_current_aggregator_name",
        },
        {
            "fieldname": "shipkia_current_aggregator_verified",
            "label": "ShipKia Current Aggregator Verified",
            "fieldtype": "Check",
            "insert_after": "shipkia_current_aggregator_raw",
        },
        {
            "fieldname": "shipkia_current_shipping_rate",
            "label": "ShipKia Current Shipping Rate",
            "fieldtype": "Currency",
            "insert_after": "shipkia_current_aggregator_verified",
        },
        {
            "fieldname": "shipkia_rto_percentage",
            "label": "ShipKia RTO Percentage",
            "fieldtype": "Percent",
            "insert_after": "shipkia_current_shipping_rate",
        },
        {
            "fieldname": "shipkia_route_column_break",
            "label": "ShipKia Route",
            "fieldtype": "Column Break",
            "insert_after": "shipkia_rto_percentage",
        },
        {
            "fieldname": "shipkia_pickup_city",
            "label": "ShipKia Pickup City",
            "fieldtype": "Data",
            "insert_after": "shipkia_route_column_break",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "shipkia_delivery_city",
            "label": "ShipKia Delivery City",
            "fieldtype": "Data",
            "insert_after": "shipkia_pickup_city",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "shipkia_average_weight",
            "label": "ShipKia Average Weight",
            "fieldtype": "Float",
            "insert_after": "shipkia_delivery_city",
        },
        {
            "fieldname": "shipkia_rate_shared",
            "label": "ShipKia Rate Shared",
            "fieldtype": "Check",
            "insert_after": "shipkia_average_weight",
        },
        {
            "fieldname": "shipkia_ai_qualification_score",
            "label": "ShipKia AI Qualification Score",
            "fieldtype": "Float",
            "insert_after": "shipkia_rate_shared",
            "precision": "2",
        },
        {
            "fieldname": "shipkia_ai_messages_count",
            "label": "ShipKia AI Messages Count",
            "fieldtype": "Int",
            "insert_after": "shipkia_ai_qualification_score",
        },
        {
            "fieldname": "shipkia_ai_details_collected",
            "label": "ShipKia AI Details Collected",
            "fieldtype": "Int",
            "insert_after": "shipkia_ai_messages_count",
        },
        {
            "fieldname": "shipkia_ai_last_message_at",
            "label": "ShipKia AI Last Message At",
            "fieldtype": "Datetime",
            "insert_after": "shipkia_ai_details_collected",
        },
        {
            "fieldname": "shipkia_context_updated_at",
            "label": "ShipKia Context Updated At",
            "fieldtype": "Datetime",
            "insert_after": "shipkia_ai_last_message_at",
        },
        {
            "fieldname": "shipkia_context_completed",
            "label": "ShipKia Context Completed",
            "fieldtype": "Check",
            "insert_after": "shipkia_context_updated_at",
        },
        {
            "fieldname": "shipkia_ai_context_complete",
            "label": "ShipKia AI Context Complete",
            "fieldtype": "Check",
            "insert_after": "shipkia_context_completed",
        },
        {
            "fieldname": "shipkia_requirement_details",
            "label": "ShipKia Requirement Details",
            "fieldtype": "Small Text",
            "insert_after": "shipkia_ai_context_complete",
        },
        {
            "fieldname": "shipkia_status_column_break",
            "label": "ShipKia Status",
            "fieldtype": "Column Break",
            "insert_after": "shipkia_requirement_details",
        },
        {
            "fieldname": "shipkia_lead_temperature",
            "label": "ShipKia Lead Temperature",
            "fieldtype": "Select",
            "insert_after": "shipkia_status_column_break",
            "options": "Cold\nWarm\nHot",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "shipkia_ai_lead_temperature",
            "label": "ShipKia AI Lead Temperature",
            "fieldtype": "Select",
            "insert_after": "shipkia_lead_temperature",
            "options": "Cold\nWarm\nHot",
        },
        {
            "fieldname": "shipkia_qualification_status",
            "label": "ShipKia Qualification Status",
            "fieldtype": "Data",
            "insert_after": "shipkia_ai_lead_temperature",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "shipkia_ai_qualification_status",
            "label": "ShipKia AI Qualification Status",
            "fieldtype": "Data",
            "insert_after": "shipkia_qualification_status",
        },
        {
            "fieldname": "shipkia_ai_onboarding_stage",
            "label": "ShipKia AI Onboarding Stage",
            "fieldtype": "Data",
            "insert_after": "shipkia_ai_qualification_status",
        },
        {
            "fieldname": "shipkia_ai_onboarding_assisted",
            "label": "ShipKia AI Onboarding Assisted",
            "fieldtype": "Check",
            "insert_after": "shipkia_ai_onboarding_stage",
        },
        {
            "fieldname": "shipkia_sales_stage",
            "label": "ShipKia Sales Stage",
            "fieldtype": "Data",
            "insert_after": "shipkia_ai_onboarding_assisted",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "shipkia_lead_source",
            "label": "ShipKia Lead Source",
            "fieldtype": "Data",
            "insert_after": "shipkia_sales_stage",
        },
        {
            "fieldname": "shipkia_first_contact_channel",
            "label": "ShipKia First Contact Channel",
            "fieldtype": "Data",
            "insert_after": "shipkia_lead_source",
        },
        {
            "fieldname": "shipkia_service_scope_status",
            "label": "ShipKia Service Scope Status",
            "fieldtype": "Data",
            "insert_after": "shipkia_first_contact_channel",
            "in_standard_filter": 1,
        },
        {
            "fieldname": "shipkia_disqualification_reason",
            "label": "ShipKia Disqualification Reason",
            "fieldtype": "Small Text",
            "insert_after": "shipkia_service_scope_status",
        },
        {
            "fieldname": "shipkia_max_shipment_weight_kg",
            "label": "ShipKia Maximum Shipment Weight (kg)",
            "fieldtype": "Float",
            "insert_after": "shipkia_disqualification_reason",
        },
    ]
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
        # Keep ShipKia qualification fields identical on both lead DocTypes so
        # one WhatsApp identity can be represented and updated in both lists.
        custom_fields[doctype].extend(shipkia_lead_fields)
    custom_fields = _only_missing_custom_fields(custom_fields)
    if custom_fields:
        create_custom_fields(custom_fields, update=True)


def _only_missing_custom_fields(custom_fields: dict[str, list[dict]]) -> dict[str, list[dict]]:
    missing_fields = {}
    for doctype, fields in (custom_fields or {}).items():
        for field in fields or []:
            fieldname = field.get("fieldname")
            if not fieldname:
                continue
            existing = frappe.db.get_value(
                "Custom Field",
                {"dt": doctype, "fieldname": fieldname},
                "name",
            )
            if not existing:
                missing_fields.setdefault(doctype, []).append(field)
    return missing_fields


def ensure_shipkia_business_type_options() -> None:
    """Keep legacy Select fields compatible without replacing user-defined options."""
    required_options = ("B2C", "D2C")
    for doctype in ("Lead", "CRM Lead"):
        custom_field = frappe.db.get_value(
            "Custom Field",
            {"dt": doctype, "fieldname": "shipkia_business_type"},
            ["name", "fieldtype", "options"],
            as_dict=True,
        )
        if not custom_field or custom_field.fieldtype != "Select":
            continue
        options = [
            option.strip()
            for option in str(custom_field.options or "").splitlines()
            if option.strip()
        ]
        merged = [*options, *(option for option in required_options if option not in options)]
        if merged == options:
            continue
        frappe.db.set_value(
            "Custom Field",
            custom_field.name,
            "options",
            "\n".join(merged),
            update_modified=False,
        )
        frappe.clear_cache(doctype=doctype)


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


def backfill_channel_account_medical_departments() -> None:
    """Copy existing account pipeline-map medical departments into the account default."""
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
        WHERE {condition}
    """
    frappe.db.sql(statement.format(condition="cca.default_medical_department IS NULL"))
    frappe.db.sql(statement.format(condition="cca.default_medical_department = ''"))


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
