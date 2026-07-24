from __future__ import annotations

import frappe
from frappe.utils import cint


CONVERSATION_JOB_DEDUPLICATION_FIELD = "enable_conversation_job_deduplication"
CONVERSATION_JOB_DEDUPLICATION_ACTIVE_FIELD = (
    "conversation_job_deduplication_setting_active"
)


def conversation_job_deduplication_enabled() -> bool:
    """Return the lead-scoring rollout policy without a deployment-time regression.

    Lead-scoring jobs were deduplicated before the setting was connected to runtime
    behavior. Until the activation patch has run, preserve that effective behavior
    even when an old site has the visible setting stored as zero.
    """
    try:
        settings = frappe.get_cached_doc("WA Chat Hub Settings")
        required_fields = {
            CONVERSATION_JOB_DEDUPLICATION_FIELD,
            CONVERSATION_JOB_DEDUPLICATION_ACTIVE_FIELD,
        }
        if not all(settings.meta.has_field(field) for field in required_fields):
            return True
        if not cint(
            getattr(settings, CONVERSATION_JOB_DEDUPLICATION_ACTIVE_FIELD, 0)
        ):
            return True
        return bool(
            cint(getattr(settings, CONVERSATION_JOB_DEDUPLICATION_FIELD, 0))
        )
    except Exception:
        # New code can be imported before migrate has synchronized settings.
        return True


def conversation_job_enqueue_options(job_id: str) -> dict[str, object]:
    """Return RQ options only when repeatable conversation work is deduplicated."""
    if not conversation_job_deduplication_enabled():
        return {}
    return {"job_id": job_id, "deduplicate": True}
