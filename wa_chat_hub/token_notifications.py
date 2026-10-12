"""Server-only OPD template adapter; callers own durable delivery/idempotency."""
import re
import frappe
from wa_chat_hub.phone_normalization import normalize_chat_phone
from wa_chat_hub.delivery_outcomes import PatientTemplateNotSentError, PatientTemplateOutcomeUnknownError


def prepare(channel, phone, patient_name, template_name, language, values, reference):
    from wa_chat_hub.services import get_or_create_contact, get_or_create_conversation
    from wa_chat_hub.interakt.template_selection import resolve_chat_template
    account = frappe.get_doc("Chat Channel Account", channel)
    if not account.is_active or account.channel_type != "Interakt":
        raise PatientTemplateNotSentError("Select an active Interakt channel.")
    phone = normalize_chat_phone(phone)
    if not re.fullmatch(r"[1-9][0-9]{9,14}", phone or "") or (phone.startswith("91") and len(phone) == 12 and phone[2] not in "6789"):
        raise PatientTemplateNotSentError("Patient mobile number is missing or invalid.")
    template = resolve_chat_template(channel, {"template_name": template_name,
        "language_code": language or "en", "body_values": values, "callback_data": reference})
    contact = get_or_create_contact(phone, patient_name)
    conversation = get_or_create_conversation(channel, contact)
    return conversation, template, phone


def send(prepared, channel, preview):
    from wa_chat_hub.outbound import send_interakt_template_message
    from wa_chat_hub.services import append_message
    conversation, template, phone = prepared
    try:
        result = send_interakt_template_message(conversation, template)
    except PatientTemplateNotSentError as exc:
        code = getattr(exc, "status_code", 0)
        if code == 408 or code >= 500:
            raise PatientTemplateOutcomeUnknownError("Provider timeout/server error; review delivery before resending.") from exc
        raise
    if not result.get("sent"):
        raise PatientTemplateNotSentError("Interakt declined the message.")
    if not result.get("provider_message_id"):
        raise PatientTemplateOutcomeUnknownError("Interakt returned no message ID; review before resending.")
    message = append_message({"conversation": conversation, "channel_account": channel,
        "phone_number": phone, "direction": "Outbound", "sender_type": "System",
        "content_type": "Template", "body": preview, "delivery_status": "Sent",
        "channel_message_id": result["provider_message_id"], "raw_transport_payload": result,
        "template_category": template.get("template_category")})
    return {"message": message.get("message"), "provider_message_id": result["provider_message_id"]}
