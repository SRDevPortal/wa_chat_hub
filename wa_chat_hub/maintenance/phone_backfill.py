from __future__ import annotations

from typing import Any
from contextlib import contextmanager
import hashlib

import frappe
from frappe.utils import cint

from wa_chat_hub.phone_normalization import canonical_phone, last10


SUPPORTED_DOCTYPES = ("CRM Lead", "Lead", "Patient", "Customer")
SOURCE_FIELDS = ("mobile_no", "mobile", "phone", "custom_whatsapp_number")
TARGET_FIELDS = (
    "vobiz_normalized_phone",
    "vobiz_mobile_last10",
    "vobiz_phone_last10",
    "vobiz_whatsapp_last10",
    "sr_mobile_norm",
)
TARGET_SOURCE_FIELDS = {
    "vobiz_normalized_phone": SOURCE_FIELDS,
    "vobiz_mobile_last10": ("mobile_no", "mobile"),
    "vobiz_phone_last10": ("phone",),
    "vobiz_whatsapp_last10": ("custom_whatsapp_number",),
    "sr_mobile_norm": ("mobile_no", "mobile"),
}
PROGRESS_PREFIX = "wa_chat_hub_phone_backfill_last_name"
BACKGROUND_BACKFILL_DOCTYPES = ("Patient", "CRM Lead", "Customer", "Lead")
BACKGROUND_BACKFILL_JOB_ID = "wa_chat_hub_indexed_phone_backfill"


def _progress_key(doctype: str) -> str:
    return f"{PROGRESS_PREFIX}_{frappe.scrub(doctype)}"


def _existing_fields(doctype: str, candidates) -> list[str]:
    meta = frappe.get_meta(doctype)
    return [fieldname for fieldname in candidates if meta.has_field(fieldname)]


def _first_value(row: Any, fields) -> str:
    for fieldname in fields:
        value = str(row.get(fieldname) or "").strip()
        if value:
            return value
    return ""


def normalized_values(row: Any, target_fields: list[str]) -> dict[str, str]:
    mobile = _first_value(row, ("mobile_no", "mobile"))
    phone = _first_value(row, ("phone",))
    whatsapp = _first_value(row, ("custom_whatsapp_number",))
    preferred = _first_value(row, SOURCE_FIELDS)

    candidates = {
        "vobiz_normalized_phone": canonical_phone(preferred),
        "vobiz_mobile_last10": last10(mobile),
        "vobiz_phone_last10": last10(phone),
        "vobiz_whatsapp_last10": last10(whatsapp),
        "sr_mobile_norm": last10(mobile),
    }
    return {fieldname: candidates[fieldname] for fieldname in target_fields}


def sync_phone_keys(doc, method=None) -> None:
    """Keep normalized keys current whenever a supported contact record is saved."""
    if doc.doctype not in SUPPORTED_DOCTYPES:
        return
    target_fields = [fieldname for fieldname in TARGET_FIELDS if doc.meta.has_field(fieldname)]
    for fieldname, value in normalized_values(doc, target_fields).items():
        if str(doc.get(fieldname) or "") != value:
            doc.set(fieldname, value)


@contextmanager
def _run_lock(doctype):
    # Connection-scoped advisory lock survives batch commits and has no expiring lease.
    scope = str(frappe.local.site) + ":phone-backfill:" + doctype
    key = "phone:" + hashlib.sha256(scope.encode()).hexdigest()[:56]
    acquired = frappe.db.sql("SELECT GET_LOCK(%s, 0)", (key,))[0][0] == 1
    try:
        yield acquired
    finally:
        if acquired:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (key,))


def _schema(doctype):
    if doctype not in SUPPORTED_DOCTYPES:
        raise ValueError("Unsupported phone backfill doctype")
    if not frappe.db.exists("DocType", doctype):
        raise ValueError("Required DocType is absent: " + doctype)
    sources = _existing_fields(doctype, SOURCE_FIELDS)
    targets = _existing_fields(doctype, TARGET_FIELDS)
    if not sources or not targets:
        raise ValueError("Required phone source or target fields are absent")
    return sources, targets


def phone_backfill_status(doctype="CRM Lead"):
    frappe.only_for("System Manager")
    sources, targets = _schema(doctype)
    return dict(doctype=doctype, last_name=str(frappe.db.get_global(_progress_key(doctype)) or ""),
                source_fields=sources, target_fields=targets, activation="manual")


def backfill_phone_keys(doctype="CRM Lead", batch_size=200, max_batches=1, dry_run=False):
    """Explicit Bench command. Do not call within an application transaction.

    A blocked batch is rolled back with its checkpoint unchanged; retry the same command.
    No lookup flag is enabled and raw source phone fields are never modified.
    """
    frappe.only_for("System Manager")
    return _backfill_phone_keys(doctype=doctype, batch_size=batch_size,
                               max_batches=max_batches, dry_run=dry_run)


def _backfill_phone_keys(*, doctype, batch_size, max_batches, dry_run=False):
    sources, targets = _schema(doctype)
    batch_size = max(1, min(cint(batch_size) or 200, 500))
    max_batches = max(1, min(cint(max_batches) or 1, 10))
    dry_run = bool(cint(dry_run))
    with _run_lock(doctype) as acquired:
        if not acquired:
            return dict(doctype=doctype, status="busy", done=False)
        cursor = str(frappe.db.get_global(_progress_key(doctype)) or "")
        old_timeout = frappe.db.sql("SELECT @@SESSION.innodb_lock_wait_timeout")[0][0]
        processed = updated = 0
        done = False
        fields = ["name", *sources, *targets]
        column_sql = ", ".join("`" + field + "`" for field in fields)
        try:
            if not dry_run:
                frappe.db.sql("SET SESSION innodb_lock_wait_timeout=3")
            for _ in range(max_batches):
                rows = frappe.get_all(doctype, filters={"name": [">", cursor]} if cursor else None,
                                      fields=fields, order_by="name asc", limit_page_length=batch_size)
                if not rows:
                    done = True
                    break
                batch_updated = 0
                blocked_name = None
                try:
                    for original in rows:
                        blocked_name = original.name
                        if dry_run:
                            row = original
                        else:
                            # Current locking read: never write keys calculated from a stale source snapshot.
                            latest = frappe.db.sql(
                                f"SELECT {column_sql} FROM `tab{doctype}` WHERE name=%s FOR UPDATE",
                                (original.name,), as_dict=True)
                            if not latest:
                                continue  # Concurrent deletion needs no repair.
                            row = latest[0]
                        changed = {key: value for key, value in normalized_values(row, targets).items()
                                   if str(row.get(key) or "") != value}
                        if changed:
                            if not dry_run:
                                frappe.db.set_value(doctype, row.name, changed, update_modified=False)
                            batch_updated += 1
                    next_cursor = rows[-1].name
                    if not dry_run:
                        frappe.db.set_global(_progress_key(doctype), next_cursor)
                        frappe.db.commit()
                except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
                    frappe.db.rollback()
                    return dict(doctype=doctype, status="retry", done=False, last_name=cursor,
                                blocked_name=blocked_name, processed=processed, updated=updated,
                                reason="Batch rolled back; retry without resetting progress")
                except Exception:
                    if not dry_run:
                        frappe.db.rollback()
                    raise
                cursor = next_cursor
                processed += len(rows)
                updated += batch_updated
                done = len(rows) < batch_size
                if done or dry_run:
                    break
            return dict(doctype=doctype, status="complete" if done else "progress", dry_run=dry_run,
                        processed=processed, updated=updated, last_name=cursor, done=done,
                        source_fields=sources, target_fields=targets, activation="manual")
        finally:
            if not dry_run:
                frappe.db.sql("SET SESSION innodb_lock_wait_timeout=%s", (old_timeout,))


def run_indexed_phone_backfill(doctype_index=0):
    """Compatibility entry point for already queued jobs: no writes or re-enqueue."""
    return dict(done=False, status="manual_backfill_required", indexed_lookup_enabled=False)


def reset_phone_backfill(doctype="CRM Lead"):
    frappe.only_for("System Manager")
    _schema(doctype)
    with _run_lock(doctype) as acquired:
        if not acquired:
            raise RuntimeError("Backfill is running; checkpoint was not reset")
        previous = str(frappe.db.get_global(_progress_key(doctype)) or "")
        frappe.db.set_global(_progress_key(doctype), "")
        frappe.db.commit()
    return dict(doctype=doctype, reset=True, previous_last_name=previous)


def verify_phone_keys(doctype="CRM Lead", after_name="", batch_size=200):
    """Read-only page verification. Sum counters over pages; not a consistent global snapshot."""
    frappe.only_for("System Manager")
    sources, targets = _schema(doctype)
    batch_size = max(1, min(cint(batch_size) or 200, 500))
    rows = frappe.get_all(doctype, filters={"name": [">", after_name]} if after_name else None,
                          fields=["name", *sources, *targets], order_by="name asc",
                          limit_page_length=batch_size)
    counts = {field: dict(missing=0, mismatched=0, invalid_source=0, not_applicable=0)
              for field in targets}
    for row in rows:
        expected = normalized_values(row, targets)
        for field in targets:
            raw = _first_value(row, tuple(s for s in TARGET_SOURCE_FIELDS[field] if s in sources))
            actual = str(row.get(field) or "")
            if not raw:
                counts[field]["not_applicable"] += 1
                if actual:
                    counts[field]["mismatched"] += 1
            elif len(last10(raw)) != 10:
                counts[field]["invalid_source"] += 1
            elif not actual and expected[field]:
                counts[field]["missing"] += 1
            elif actual != expected[field]:
                counts[field]["mismatched"] += 1
    return dict(doctype=doctype, processed=len(rows), counts=counts,
                last_name=rows[-1].name if rows else after_name, done=len(rows) < batch_size,
                source_fields=sources, target_fields=targets,
                validation="Existing normalization rules; international semantics require separate review")


@frappe.whitelist()
def phone_key_coverage(doctype: str = "CRM Lead") -> dict[str, Any]:
    """Report missing normalized keys without scanning raw payload columns."""
    frappe.only_for("System Manager")
    if doctype not in SUPPORTED_DOCTYPES:
        frappe.throw(f"Unsupported phone coverage doctype: {doctype}")
    if not frappe.db.exists("DocType", doctype):
        return {"doctype": doctype, "total": 0, "reason": "doctype_missing"}

    source_fields = _existing_fields(doctype, SOURCE_FIELDS)
    target_fields = _existing_fields(doctype, TARGET_FIELDS)
    table = f"`tab{doctype}`"
    result: dict[str, Any] = {
        "doctype": doctype,
        "total": frappe.db.count(doctype),
        "source_fields": source_fields,
        "target_fields": target_fields,
        "missing": {},
    }
    for fieldname in target_fields:
        relevant_sources = [
            source
            for source in TARGET_SOURCE_FIELDS[fieldname]
            if source in source_fields
        ]
        if not relevant_sources:
            result["missing"][fieldname] = 0
            continue
        has_source = " OR ".join(
            f"COALESCE(`{source}`, '') != ''" for source in relevant_sources
        )
        count = frappe.db.sql(
            f"""SELECT COUNT(*) FROM {table}
                WHERE ({has_source}) AND COALESCE(`{fieldname}`, '') = ''""",
        )[0][0]
        result["missing"][fieldname] = count
    return result
