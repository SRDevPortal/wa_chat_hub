from __future__ import annotations


def execute() -> None:
    from wa_chat_hub.migrate import backfill_indexed_phone_keys

    backfill_indexed_phone_keys()
