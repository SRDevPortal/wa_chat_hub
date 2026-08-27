from __future__ import annotations


def execute() -> None:
    from wa_chat_hub.setup_agents import backfill_conversation_identities

    backfill_conversation_identities()
