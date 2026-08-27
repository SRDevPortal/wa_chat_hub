from __future__ import annotations

import io
import json
import shutil
import subprocess
import traceback
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import frappe
from frappe import _
from frappe.utils import now_datetime


UPDATE_DOCTYPE = "WA App Update Script"
LOG_DOCTYPE = "WA App Update Log"
MAX_RESULT_LENGTH = 20000
MIGRATION_TIMEOUT_SECONDS = 900


def _require_system_manager() -> None:
    frappe.only_for("System Manager")
    settings = frappe.get_single("WA Chat Hub Settings")
    enabled = getattr(settings, "enable_app_update_system", None)
    if enabled is not None and not int(enabled or 0):
        frappe.throw(_("App Updates are disabled in WA Chat Hub Settings."))


def _bounded(value: Any, limit: int = MAX_RESULT_LENGTH) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str, indent=2)
        except (TypeError, ValueError):
            value = str(value)
    if len(value) <= limit:
        return value
    return value[:limit] + "\n...[output truncated]"


def execute_python_script(script: str, update_doc=None) -> str:
    """Execute an approved update script and return bounded stdout/result text."""
    app_path = Path(frappe.get_app_path("wa_chat_hub")).resolve()
    bench_path = app_path.parents[2]
    output = io.StringIO()
    namespace = {
        "__name__": "__wa_chat_hub_app_update__",
        "frappe": frappe,
        "app_path": app_path,
        "bench_path": bench_path,
        "settings": frappe.get_single("WA Chat Hub Settings"),
        "update": update_doc,
        "result": None,
    }
    compiled = compile(script or "", f"<WA App Update: {getattr(update_doc, 'name', 'script')}>", "exec")
    with redirect_stdout(output):
        exec(compiled, namespace, namespace)

    parts = []
    stdout = output.getvalue().strip()
    if stdout:
        parts.append(stdout)
    if namespace.get("result") is not None:
        parts.append(_bounded(namespace["result"]))
    return _bounded("\n".join(parts) or "Completed successfully.")


def write_update_log(
    update_doc,
    *,
    action: str,
    success: bool,
    result: Any = None,
    error: Any = None,
    script_snapshot: str | None = None,
) -> str:
    log = frappe.get_doc(
        {
            "doctype": LOG_DOCTYPE,
            "update_script": update_doc.name,
            "update_version": update_doc.update_version,
            "action": action,
            "success": int(bool(success)),
            "executed_by": frappe.session.user,
            "executed_at": now_datetime(),
            "restart_required": int(bool(update_doc.restart_required)),
            "script_snapshot": _bounded(script_snapshot),
            "result": _bounded(result),
            "error": _bounded(error),
        }
    )
    log.insert(ignore_permissions=True)
    return log.name


def _set_execution_state(name: str, status: str) -> None:
    current_status = frappe.db.get_value(UPDATE_DOCTYPE, name, "status")
    if current_status in {"Applying", "Reverting"}:
        frappe.throw(_("Update {0} is already being executed.").format(name))
    frappe.db.set_value(UPDATE_DOCTYPE, name, "status", status, update_modified=True)
    frappe.db.commit()


def _queue_migration(update_doc) -> bool:
    if not update_doc.run_migration:
        return False
    try:
        frappe.enqueue(
            "wa_chat_hub.app_update.run_site_migration",
            queue="long",
            enqueue_after_commit=True,
            job_id=f"wa-chat-hub-update-migrate::{frappe.local.site}::{update_doc.name}",
            deduplicate=True,
            site=frappe.local.site,
            update_name=update_doc.name,
            update_version=update_doc.update_version,
            restart_required=int(bool(update_doc.restart_required)),
        )
        return True
    except Exception:
        write_update_log(
            update_doc,
            action="Failed",
            success=False,
            error="Update applied, but the optional migration could not be queued.\n"
            + _bounded(traceback.format_exc()),
        )
        frappe.db.commit()
        return False


def _execute_action(name: str, *, action: str, script_field: str, enabled: int, status: str) -> dict:
    _require_system_manager()
    update_doc = frappe.get_doc(UPDATE_DOCTYPE, name)
    transient_status = "Reverting" if script_field == "revert_script" else "Applying"
    _set_execution_state(name, transient_status)
    script = update_doc.get(script_field) or ""

    try:
        result = execute_python_script(script, update_doc)
        timestamp = now_datetime()
        values = {
            "enabled": enabled,
            "status": status,
            "last_execution_result": result,
        }
        if script_field == "apply_script":
            values.update(last_applied_by=frappe.session.user, last_applied_at=timestamp)
        else:
            values.update(last_reverted_by=frappe.session.user, last_reverted_at=timestamp)
        frappe.db.set_value(UPDATE_DOCTYPE, name, values, update_modified=True)
        update_doc.reload()
        write_update_log(
            update_doc,
            action=action,
            success=True,
            result=result,
            script_snapshot=script,
        )
        frappe.db.commit()
    except Exception as exc:
        frappe.db.rollback()
        update_doc = frappe.get_doc(UPDATE_DOCTYPE, name)
        error = _bounded(traceback.format_exc())
        frappe.db.set_value(
            UPDATE_DOCTYPE,
            name,
            {"status": "Failed", "last_execution_result": error},
            update_modified=True,
        )
        write_update_log(
            update_doc,
            action="Failed",
            success=False,
            error=error,
            script_snapshot=script,
        )
        frappe.db.commit()
        frappe.log_error(title=f"WA App Update failed: {name}", message=error)
        frappe.throw(_("Update execution failed: {0}").format(str(exc)))

    if update_doc.clear_cache_after_execution:
        try:
            frappe.clear_cache()
        except Exception:
            frappe.log_error(
                title=f"WA App Update cache clear failed: {name}",
                message=frappe.get_traceback(),
            )
    migration_queued = _queue_migration(update_doc)
    return {
        "success": True,
        "status": status,
        "message": _("Update {0} completed successfully.").format(action.lower()),
        "result": result,
        "restart_required": bool(update_doc.restart_required),
        "migration_queued": migration_queued,
    }


@frappe.whitelist()
def enable_update(name: str) -> dict:
    _require_system_manager()
    enabled = frappe.db.get_value(UPDATE_DOCTYPE, name, "enabled")
    if enabled:
        frappe.throw(_("This update is already enabled. Use Run Again to execute it again."))
    return _execute_action(name, action="Enabled", script_field="apply_script", enabled=1, status="Enabled")


@frappe.whitelist()
def run_update(name: str) -> dict:
    _require_system_manager()
    enabled = frappe.db.get_value(UPDATE_DOCTYPE, name, "enabled")
    if not enabled:
        frappe.throw(_("Enable this update before using Run Again."))
    return _execute_action(name, action="Reapplied", script_field="apply_script", enabled=1, status="Enabled")


@frappe.whitelist()
def disable_update(name: str) -> dict:
    _require_system_manager()
    enabled = frappe.db.get_value(UPDATE_DOCTYPE, name, "enabled")
    if not enabled:
        frappe.throw(_("This update is already disabled."))
    return _execute_action(name, action="Disabled", script_field="revert_script", enabled=0, status="Disabled")


@frappe.whitelist()
def delete_update(name: str) -> dict:
    _require_system_manager()
    if frappe.db.get_value(UPDATE_DOCTYPE, name, "enabled"):
        _execute_action(name, action="Disabled", script_field="revert_script", enabled=0, status="Disabled")

    update_doc = frappe.get_doc(UPDATE_DOCTYPE, name)
    snapshot = update_doc.apply_script
    update_version = update_doc.update_version
    restart_required = update_doc.restart_required
    frappe.delete_doc(UPDATE_DOCTYPE, name, ignore_permissions=True)
    deleted_record = frappe._dict(
        name=name,
        update_version=update_version,
        restart_required=restart_required,
    )
    write_update_log(
        deleted_record,
        action="Deleted",
        success=True,
        result="Update definition deleted; audit logs retained.",
        script_snapshot=snapshot,
    )
    frappe.db.commit()
    return {"success": True, "message": _("Update deleted. Its execution logs were retained.")}


@frappe.whitelist()
def get_update_summary() -> dict:
    _require_system_manager()
    active_count = frappe.db.count(UPDATE_DOCTYPE, filters={"enabled": 1})
    latest = frappe.get_all(
        LOG_DOCTYPE,
        filters={},
        fields=["update_script", "action", "success", "executed_at", "result", "error"],
        order_by="executed_at desc",
        limit_start=0,
        limit_page_length=1,
    )
    return {"active_count": active_count, "latest": latest[0] if latest else None}


def run_site_migration(
    site: str,
    update_name: str,
    update_version: str = "",
    restart_required: int = 0,
) -> None:
    """Run the optional site migration in a long worker and append an audit log."""
    if frappe.db.exists(UPDATE_DOCTYPE, update_name):
        update_doc = frappe.get_doc(UPDATE_DOCTYPE, update_name)
    else:
        update_doc = frappe._dict(
            name=update_name,
            update_version=update_version,
            restart_required=restart_required,
        )
    frappe.db.commit()
    bench_command = shutil.which("bench")
    if not bench_command:
        user_bench = Path.home() / ".local" / "bin" / "bench"
        if user_bench.is_file():
            bench_command = str(user_bench)
    if not bench_command:
        error = "The bench executable is not available in the worker PATH. Run bench migrate manually."
        write_update_log(update_doc, action="Failed", success=False, error=error)
        frappe.db.commit()
        return

    app_path = Path(frappe.get_app_path("wa_chat_hub")).resolve()
    bench_path = app_path.parents[2]
    try:
        process = subprocess.run(
            [bench_command, "--site", site, "migrate"],
            cwd=str(bench_path),
            capture_output=True,
            text=True,
            timeout=MIGRATION_TIMEOUT_SECONDS,
            check=False,
        )
        output = _bounded("\n".join(part for part in [process.stdout, process.stderr] if part))
        write_update_log(
            update_doc,
            action="Migration",
            success=process.returncode == 0,
            result=output if process.returncode == 0 else None,
            error=output if process.returncode else None,
        )
        frappe.db.commit()
    except Exception:
        frappe.db.rollback()
        write_update_log(
            update_doc,
            action="Failed",
            success=False,
            error=_bounded(traceback.format_exc()),
        )
        frappe.db.commit()
