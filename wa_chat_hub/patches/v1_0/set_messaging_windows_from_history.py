"""Compatibility patch: historical backfills are explicitly manual-only.

Patch Log completion means this automatic step was retired, not that history
was processed. See docs/manual-backfills.md for bounded operator commands.
"""


def execute():
    return {"status": "manual_backfill_required"}
