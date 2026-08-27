from __future__ import annotations


def execute() -> None:
    from wa_chat_hub.migrate import backfill_channel_account_medical_departments

    backfill_channel_account_medical_departments()
