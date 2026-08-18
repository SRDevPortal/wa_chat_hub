from __future__ import annotations


def execute() -> None:
    from wa_chat_hub.migrate import ensure_app_update_indexes

    ensure_app_update_indexes()
