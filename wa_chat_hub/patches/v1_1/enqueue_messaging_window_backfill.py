from __future__ import annotations


def execute() -> None:
    from wa_chat_hub.migrate import backfill_messaging_windows

    backfill_messaging_windows()
