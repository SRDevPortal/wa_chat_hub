from __future__ import annotations

from pathlib import Path

import frappe


MODULE = "wa_chat_hub"


def after_migrate() -> None:
    """Keep WA Chat Hub standard doctypes and workspace synced after migrate."""
    sync_standard_doctypes()
    try:
        from wa_chat_hub.setup_workspace import run as setup_workspace

        setup_workspace()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Workspace Sync Failed")


def sync_standard_doctypes() -> None:
    doctype_dir = Path(__file__).parent / MODULE / "doctype"
    if not doctype_dir.exists():
        return

    for json_file in sorted(doctype_dir.glob("*/*.json")):
        doctype_name = json_file.parent.name
        try:
            frappe.reload_doc(MODULE, "doctype", doctype_name, force=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"WA Chat Hub DocType Sync Failed: {doctype_name}",
            )
