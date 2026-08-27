from __future__ import annotations


def execute() -> None:
    from wa_chat_hub.migrate import ensure_lead_scoring_fields

    ensure_lead_scoring_fields()
