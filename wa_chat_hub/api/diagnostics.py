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
