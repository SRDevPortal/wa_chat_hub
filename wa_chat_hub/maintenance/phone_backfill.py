from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import cint

from wa_chat_hub.phone_normalization import canonical_phone, last10


SUPPORTED_DOCTYPES = ("CRM Lead", "Lead", "Customer")
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
BACKGROUND_BACKFILL_DOCTYPES = ("CRM Lead", "Customer", "Lead")
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


@frappe.whitelist()
def backfill_phone_keys(
    doctype: str = "CRM Lead",
    batch_size: int = 2000,
    max_batches: int = 10,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Backfill normalized phone keys in bounded, resumable batches."""
    frappe.only_for("System Manager")
    return _backfill_phone_keys(
        doctype=doctype,
        batch_size=batch_size,
        max_batches=max_batches,
        dry_run=dry_run,
    )


def _backfill_phone_keys(
    *,
    doctype: str,
    batch_size: int,
    max_batches: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    if doctype not in SUPPORTED_DOCTYPES:
        frappe.throw(f"Unsupported phone backfill doctype: {doctype}")
    if not frappe.db.exists("DocType", doctype):
        return {"doctype": doctype, "done": True, "reason": "doctype_missing"}

    batch_size = max(100, min(cint(batch_size) or 2000, 5000))
    max_batches = max(1, min(cint(max_batches) or 10, 100))
    dry_run = bool(cint(dry_run))
    source_fields = _existing_fields(doctype, SOURCE_FIELDS)
    target_fields = _existing_fields(doctype, TARGET_FIELDS)
    if not source_fields or not target_fields:
        return {
            "doctype": doctype,
            "done": True,
            "reason": "phone_source_or_target_fields_missing",
            "source_fields": source_fields,
            "target_fields": target_fields,
        }

    progress_key = _progress_key(doctype)
    last_name = str(frappe.db.get_global(progress_key) or "")
    processed = updated = 0
    done = False

    for _batch in range(max_batches):
        filters = {"name": [">", last_name]} if last_name else None
        rows = frappe.get_all(
            doctype,
            filters=filters,
            fields=["name", *source_fields, *target_fields],
            order_by="name asc",
            limit_page_length=batch_size,
        )
        if not rows:
            done = True
            break

        updates = {}
        for row in rows:
            processed += 1
            values = normalized_values(row, target_fields)
            changed = {
                fieldname: value
                for fieldname, value in values.items()
                if str(row.get(fieldname) or "") != value
            }
            if changed:
                updates[row.name] = changed

        updated += len(updates)
        last_name = rows[-1].name
        if not dry_run:
            if updates:
                frappe.db.bulk_update(
                    doctype,
                    updates,
                    chunk_size=min(200, batch_size),
                    update_modified=False,
                )
            frappe.db.set_global(progress_key, last_name)
            frappe.db.commit()

        if len(rows) < batch_size:
            done = True
            break
        if dry_run:
            break

    return {
        "doctype": doctype,
        "dry_run": dry_run,
        "processed": processed,
        "updated": updated,
        "last_name": last_name,
        "done": done,
        "source_fields": source_fields,
        "target_fields": target_fields,
    }


def run_indexed_phone_backfill(doctype_index: int = 0) -> dict[str, Any]:
    """Background-safe, throttled backfill; enable exact lookup only after completion."""
    doctype_index = max(0, cint(doctype_index))
    if doctype_index >= len(BACKGROUND_BACKFILL_DOCTYPES):
        frappe.db.set_single_value(
            "WA Chat Hub Settings",
            "enable_indexed_phone_lookup",
            1,
        )
        frappe.db.commit()
        return {"done": True, "indexed_lookup_enabled": True}

    doctype = BACKGROUND_BACKFILL_DOCTYPES[doctype_index]
    result = _backfill_phone_keys(
        doctype=doctype,
        batch_size=1000,
        max_batches=1,
    )
    next_index = doctype_index + 1 if result.get("done") else doctype_index
    frappe.enqueue(
        "wa_chat_hub.maintenance.phone_backfill.run_indexed_phone_backfill",
        queue="long",
        timeout=1800,
        enqueue_after_commit=True,
        job_id=f"{BACKGROUND_BACKFILL_JOB_ID}_{next_index}",
        deduplicate=True,
        doctype_index=next_index,
    )
    return {
        **result,
        "done": False,
        "next_doctype_index": next_index,
    }


@frappe.whitelist()
def reset_phone_backfill(doctype: str = "CRM Lead") -> dict[str, Any]:
    frappe.only_for("System Manager")
    if doctype not in SUPPORTED_DOCTYPES:
        frappe.throw(f"Unsupported phone backfill doctype: {doctype}")
    frappe.db.set_global(_progress_key(doctype), "")
    frappe.db.commit()
    return {"doctype": doctype, "reset": True}


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
