from __future__ import annotations

import re

import frappe

from wa_chat_hub.services import _is_remote_url, _repair_remote_attachment_comment_url


def execute() -> None:
    widen_file_url_column()
    repair_chat_message_files()
    repair_crm_lead_files()


def widen_file_url_column() -> None:
    if not frappe.db.exists("DocType", "File") or not frappe.db.has_column("File", "file_url"):
        return

    try:
        if frappe.db.db_type == "mariadb":
            frappe.db.change_column_type("File", "file_url", "longtext", nullable=True)
        elif frappe.db.db_type == "postgres":
            frappe.db.sql_ddl('ALTER TABLE "tabFile" ALTER COLUMN "file_url" TYPE text')
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub File URL Column Patch Failed")


def repair_chat_message_files() -> None:
    if not frappe.db.exists("DocType", "Chat Message"):
        return

    rows = frappe.get_all(
        "Chat Message",
        filters={
            "media_url": ["like", "http%"],
            "attachment_file": ["is", "set"],
        },
        fields=["name", "media_url", "attachment_file"],
        limit_page_length=0,
    )
    for row in rows:
        media_url = str(row.media_url or "").strip()
        if not _is_remote_url(media_url):
            continue
        file_row = frappe.db.get_value("File", row.attachment_file, ["file_name", "file_url"], as_dict=True)
        if not file_row:
            continue
        if file_row.file_url != media_url:
            frappe.db.set_value("File", row.attachment_file, "file_url", media_url, update_modified=False)
        _repair_remote_attachment_comment_url("Chat Message", row.name, file_row.file_name, media_url)


def repair_crm_lead_files() -> None:
    if not frappe.db.exists("DocType", "CRM Lead"):
        return

    rows = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "CRM Lead",
            "file_name": ["like", "WA-%"],
        },
        fields=["name", "file_name", "file_url", "attached_to_name"],
        limit_page_length=0,
    )
    for row in rows:
        message_name = _extract_message_name(row.file_name)
        if not message_name:
            continue

        media_url = frappe.db.get_value("Chat Message", message_name, "media_url")
        media_url = str(media_url or "").strip()
        if not _is_remote_url(media_url):
            continue
        if row.file_url != media_url:
            frappe.db.set_value("File", row.name, "file_url", media_url, update_modified=False)
        _repair_remote_attachment_comment_url("CRM Lead", row.attached_to_name, row.file_name, media_url)


def _extract_message_name(file_name: str) -> str | None:
    match = re.match(r"^WA-(.+?)-", str(file_name or ""))
    return match.group(1) if match else None
