from __future__ import annotations

import time
from typing import Callable, TypeVar

import frappe

from wa_chat_hub.task_logger import task_log


LOCK_RETRY_ATTEMPTS = 3
LOCK_RETRY_DELAY_SECONDS = 0.35
_T = TypeVar("_T")


def is_db_lock_conflict(exc: Exception) -> bool:
    if isinstance(exc, (frappe.QueryTimeoutError, frappe.QueryDeadlockError)):
        return True
    cause = getattr(exc, "__cause__", None)
    code = getattr(cause, "args", [None])[0] if cause else None
    return code in {1205, 1213}


def with_db_lock_retry(
    label: str,
    action: Callable[[], _T],
    *,
    attempts: int = LOCK_RETRY_ATTEMPTS,
) -> _T:
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:
            if not is_db_lock_conflict(exc) or attempt >= attempts:
                raise
            task_log(
                "db",
                "lock_retry",
                label=label,
                attempt=attempt,
                error=str(exc)[:140],
            )
            time.sleep(LOCK_RETRY_DELAY_SECONDS * attempt)

    raise RuntimeError(f"DB lock retry exhausted for {label}")
