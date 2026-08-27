from __future__ import annotations

import frappe


INDEXES = (
    (
        "tabPatient Encounter",
        "idx_patient_encounter_patient_timeline",
        (
            "ALTER TABLE `tabPatient Encounter` "
            "ADD INDEX `idx_patient_encounter_patient_timeline` "
            "(`patient`, `encounter_date`, `encounter_time`, `modified`), "
            "ALGORITHM=INPLACE, LOCK=NONE"
        ),
    ),
    (
        "tabPatient Medical Record",
        "idx_patient_medical_record_patient_timeline",
        (
            "ALTER TABLE `tabPatient Medical Record` "
            "ADD INDEX `idx_patient_medical_record_patient_timeline` "
            "(`patient`, `communication_date`, `modified`), "
            "ALGORITHM=INPLACE, LOCK=NONE"
        ),
    ),
)


def execute() -> None:
    for table, index_name, alter_sql in INDEXES:
        if not _table_exists(table) or _index_exists(table, index_name):
            continue
        frappe.db.sql(alter_sql)


def _table_exists(table: str) -> bool:
    return bool(
        frappe.db.sql(
            """
            SELECT 1
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            LIMIT 1
            """,
            table,
        )
    )


def _index_exists(table: str, index_name: str) -> bool:
    return bool(
        frappe.db.sql(
            """
            SELECT 1
            FROM information_schema.STATISTICS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND INDEX_NAME = %s
            LIMIT 1
            """,
            (table, index_name),
        )
    )
