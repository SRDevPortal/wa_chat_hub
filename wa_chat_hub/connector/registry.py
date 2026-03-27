from wa_chat_hub.connector.official.adapter import OfficialWhatsAppAdapter
from wa_chat_hub.connector.personal.adapter import PersonalWhatsAppAdapter


REGISTRY = {
    "Official WhatsApp": OfficialWhatsAppAdapter(),
    "Personal WhatsApp": PersonalWhatsAppAdapter(),
}


def get_adapter(channel_type: str):
    return REGISTRY[channel_type]
