from __future__ import annotations


def execute() -> None:
    from wa_chat_hub.migrate import migrate_conversation_crm_lead_links

    migrate_conversation_crm_lead_links()
