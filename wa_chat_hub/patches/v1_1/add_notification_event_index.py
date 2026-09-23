from wa_chat_hub.maintenance.notification_indexes import ensure_notification_event_index


def execute():
    ensure_notification_event_index()
