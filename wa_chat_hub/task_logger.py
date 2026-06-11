from __future__ import annotations

import time
from typing import Any
import os

import frappe
from frappe.utils import get_datetime, now_datetime


def elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 2)


def queue_wait_seconds(creation: Any) -> float | None:
    try:
        if not creation:
            return None
        return round((now_datetime() - get_datetime(creation)).total_seconds(), 2)
    except Exception:
        return None


def task_log(task: str, event: str, **fields) -> None:
    try:
        details = " ".join(
            f"{key}={value}" for key, value in fields.items() if value is not None
        )
        message = f"{task} {event} {details}".strip()
        if _console_logging_enabled():
            print(f"[wa_chat_hub] {message}", flush=True)
        frappe.logger("wa_chat_hub", allow_site=True).info(message)
    except Exception:
        pass


def _console_logging_enabled() -> bool:
    value = (
        getattr(getattr(frappe, "conf", None), "wa_chat_hub_console_log", None)
        or os.environ.get("WA_CHAT_HUB_CONSOLE_LOG")
    )
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
