from __future__ import annotations

import frappe


TABLE = "tabWA Channel Pipeline Map"
INDEX_NAME = "idx_wa_pipeline_map_department_active"
FIELDS = ("sr_medical_department", "is_active")


def execute() -> None:
    if not _table_exists() or _index_exists():
        return
    if not all(frappe.db.has_column("WA Channel Pipeline Map", fieldname) for fieldname in FIELDS):
        return

    base_sql = (
        f"ALTER TABLE `{TABLE}` ADD INDEX `{INDEX_NAME}` "
        "(`sr_medical_department`, `is_active`)"
    )
    try:
        frappe.db.sql(f"{base_sql}, ALGORITHM=INPLACE, LOCK=NONE")
    except Exception:
        # Older MariaDB/table combinations may not support online DDL. Recheck
        # first because interrupted DDL can still leave the index installed.
        if not _index_exists():
            frappe.db.sql(base_sql)


def _table_exists() -> bool:
    return bool(
        frappe.db.sql(
            """
            SELECT 1 FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            LIMIT 1
            """,
            TABLE,
        )
    )


def _index_exists() -> bool:
    return bool(
        frappe.db.sql(
            """
            SELECT 1 FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s
            LIMIT 1
            """,
            (TABLE, INDEX_NAME),
        )
    )
