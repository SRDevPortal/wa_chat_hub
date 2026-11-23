"""Bounded, synchronous maintenance shared with Shipment Tracking (a dependent app).

No scheduler or migration hook calls this module. Cursors are explicit: save the
returned last_name and pass it as after_name on the next operator invocation.
"""
from __future__ import annotations

import hashlib

import frappe


def run_batch(*, operation, doctype, process_row, batch_size=100, after_name="",
              dry_run=True, filters=None):
    if frappe.flags.in_migrate or frappe.flags.in_install:
        raise RuntimeError("Historical backfills cannot run during migration or installation")
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a JSON boolean")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 500:
        raise ValueError("batch_size must be an integer between 1 and 500")
    if not isinstance(after_name, str):
        raise ValueError("after_name must be a string")
    if not frappe.db.exists("DocType", doctype):
        raise ValueError(f"Missing required DocType: {doctype}")
    filters = dict(filters or {})
    filters["name"] = [">", after_name]
    lock = hashlib.sha256(f"{frappe.local.site}:manual-history:{operation}".encode()).hexdigest()
    acquired = False
    try:
        if not dry_run:
            acquired = bool(frappe.db.sql("SELECT GET_LOCK(%s, 0)", (lock,))[0][0])
            if not acquired:
                return {"status": "busy", "last_name": after_name, "done": False, "processed": 0}
        names = frappe.get_all(doctype, filters=filters, pluck="name",
                               order_by="name asc", limit_page_length=batch_size + 1)
        batch = names[:batch_size]
        processed = 0
        if not dry_run:
            for name in batch:
                # Doctype is an internal, fixed task definition, never user input.
                if frappe.db.sql(f"SELECT name FROM `tab{doctype}` WHERE name=%s FOR UPDATE", (name,)):
                    process_row(name)
                    processed += 1
            frappe.db.commit()
        return {
            "status": "preview" if dry_run else "completed_batch",
            "operation": operation,
            "selected": len(batch),
            "processed": processed,
            "last_name": batch[-1] if batch and not dry_run else after_name,
            "has_more": len(names) > batch_size,
            "done": not dry_run and len(names) <= batch_size,
        }
    except Exception:
        if not dry_run:
            frappe.db.rollback()
        raise
    finally:
        if acquired:
            frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock,))
