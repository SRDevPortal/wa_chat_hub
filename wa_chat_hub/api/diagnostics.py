from __future__ import annotations

import frappe
from frappe import _


def _ensure_diagnostic_access() -> None:
    roles = set(frappe.get_roles())
    if "System Manager" in roles:
        return
    if frappe.db.exists("Role", "WA Chat Manager") and "WA Chat Manager" in roles:
        return
    frappe.throw(_("Not permitted to view WA Chat Hub diagnostics"), frappe.PermissionError)


@frappe.whitelist()
def get_latest_inbound_media(limit: int = 10):
    """Return recent inbound media messages with linked File state for quick debugging."""
    _ensure_diagnostic_access()
    limit = max(1, min(int(limit or 10), 50))

    messages = frappe.get_all(
        "Chat Message",
        filters={
            "direction": "Inbound",
            "content_type": ["in", ["Image", "Document", "Audio", "Video", "Sticker"]],
        },
        fields=[
            "name",
            "creation",
            "conversation",
            "content_type",
            "body",
            "media_url",
            "attachment_file",
            "delivery_status",
            "channel_message_id",
        ],
        order_by="creation desc",
        limit_page_length=limit,
    )

    for row in messages:
        file_url = ""
        file_name = ""
        if row.get("attachment_file"):
            file_data = frappe.db.get_value(
                "File",
                row.get("attachment_file"),
                ["file_name", "file_url"],
                as_dict=True,
            )
            if file_data:
                file_name = file_data.file_name or ""
                file_url = file_data.file_url or ""

        row["file_name"] = file_name
        row["file_url"] = file_url
        row["has_media_url"] = bool(str(row.get("media_url") or "").strip())
        row["has_attachment_file"] = bool(row.get("attachment_file"))
        row["file_url_matches_media_url"] = bool(row.get("media_url")) and row.get("media_url") == file_url

    return {"success": True, "result": messages}


@frappe.whitelist()
def get_latest_crm_lead_media_files(limit: int = 10):
    """Return recent WA media File rows attached to CRM Lead records."""
    _ensure_diagnostic_access()
    limit = max(1, min(int(limit or 10), 50))

    rows = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "CRM Lead",
            "file_name": ["like", "WA-%"],
        },
        fields=["name", "creation", "file_name", "file_url", "attached_to_name"],
        order_by="creation desc",
        limit_page_length=limit,
    )
    for row in rows:
        row["is_remote_url"] = str(row.get("file_url") or "").startswith(("http://", "https://"))
        row["comment_href"] = _latest_attachment_comment_href("CRM Lead", row.get("attached_to_name"), row.get("file_name"))
        row["comment_href_matches_file_url"] = bool(row.get("file_url")) and row.get("comment_href") == row.get("file_url")
    return {"success": True, "result": rows}


@frappe.whitelist()
def get_crm_lead_files(lead: str):
    """Return File and attachment-comment URL state for one CRM Lead."""
    _ensure_diagnostic_access()
    if not lead or not frappe.db.exists("CRM Lead", lead):
        frappe.throw(_("CRM Lead not found: {0}").format(lead))

    rows = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "CRM Lead",
            "attached_to_name": lead,
        },
        fields=["name", "creation", "file_name", "file_url", "is_private"],
        order_by="creation desc",
        limit_page_length=0,
    )
    for row in rows:
        file_url = str(row.get("file_url") or "")
        row["url_kind"] = _classify_file_url(file_url)
        row["has_double_encoded_signature"] = "%25" in file_url
        row["comment_href"] = _latest_attachment_comment_href("CRM Lead", lead, row.get("file_name"))
        row["comment_href_matches_file_url"] = bool(file_url) and row.get("comment_href") == file_url
        row["comment_href_has_double_encoded_signature"] = "%25" in str(row.get("comment_href") or "")
    return {"success": True, "lead": lead, "count": len(rows), "result": rows}


def _classify_file_url(file_url: str) -> str:
    if not file_url:
        return "missing"
    lowered = file_url.lower()
    if lowered.startswith("s3://"):
        return "s3"
    if lowered.startswith(("http://", "https://")):
        return "remote"
    if lowered.startswith(("/files/", "/private/files/")):
        return "local"
    return "unknown"


def _latest_attachment_comment_href(doctype: str, name: str, file_name: str) -> str:
    if not doctype or not name:
        return ""
    comments = frappe.get_all(
        "Comment",
        filters={
            "reference_doctype": doctype,
            "reference_name": name,
            "comment_type": "Attachment",
        },
        fields=["content"],
        order_by="creation desc",
        limit_page_length=10,
    )
    marker = str(file_name or "")
    for comment in comments:
        content = str(comment.content or "")
        if marker and marker not in content:
            continue
        for quote in ("'", '"'):
            prefix = f"href={quote}"
            start = content.find(prefix)
            if start < 0:
                continue
            value_start = start + len(prefix)
            value_end = content.find(quote, value_start)
            if value_end > value_start:
                return content[value_start:value_end]
    return ""
