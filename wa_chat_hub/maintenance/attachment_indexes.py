"""Indexes for attachment lookups. Run outside business transactions."""
from __future__ import annotations

import frappe


DOCTYPE = "Chat Message"
TABLE = "tabChat Message"
INDEX_NAME = "idx_chat_message_attachment_modified"
FIELDS = ("attachment_file", "modified")


def _indexes() -> dict:
    rows = frappe.db.sql(
        """SELECT * FROM information_schema.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        ORDER BY INDEX_NAME, SEQ_IN_INDEX""",
        (TABLE,),
        as_dict=True,
    )
    indexes = {}
    for row in rows:
        indexes.setdefault(row["INDEX_NAME"], []).append(row)
    return indexes


def _equivalent_index(indexes: dict) -> str | None:
    for name, columns in indexes.items():
        leading = columns[: len(FIELDS)]
        if tuple(row["COLUMN_NAME"] for row in leading) != FIELDS:
            continue
        if any(
            row.get("SUB_PART") is not None
            or row.get("INDEX_TYPE", "BTREE") != "BTREE"
            or str(row.get("IGNORED", "NO")).upper() == "YES"
            for row in leading
        ):
            continue
        return name
    return None


def ensure_attachment_index() -> dict:
    """Add only the missing lookup index; unsupported online DDL fails closed.

    This commits before DDL. Use a migration or dedicated ``bench execute``
    session, never a request that has uncommitted business changes.
    """
    if not frappe.db.table_exists(DOCTYPE):
        return {"status": "table_absent"}
    indexes = _indexes()
    if existing := _equivalent_index(indexes):
        return {"status": "already_present", "index": existing}
    if INDEX_NAME in indexes:
        raise RuntimeError(f"{INDEX_NAME} has an unexpected definition; review it before proceeding")
    if not all(frappe.db.has_column(DOCTYPE, field) for field in FIELDS):
        raise RuntimeError("Chat Message attachment fields are missing; synchronize its schema first")

    previous_wait = frappe.db.sql("SELECT @@SESSION.lock_wait_timeout")[0][0]
    try:
        # Avoid a long metadata-lock queue behind existing transactions.
        frappe.db.sql("SET SESSION lock_wait_timeout = 5")
        frappe.db.sql_ddl(
            f"ALTER TABLE `{TABLE}` ADD INDEX `{INDEX_NAME}` "
            "(`attachment_file`, `modified`), ALGORITHM=INPLACE, LOCK=NONE"
        )
    finally:
        frappe.db.sql("SET SESSION lock_wait_timeout = %s", (previous_wait,))

    if not _equivalent_index(_indexes()):
        raise RuntimeError("Attachment index was not found after online DDL")
    return {"status": "created", "index": INDEX_NAME}
