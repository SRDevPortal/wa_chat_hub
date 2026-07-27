from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr

from wa_chat_hub.channel_resolver import get_or_create_mapped_patient_conversation
from wa_chat_hub.outbound import send_interakt_template_message
from wa_chat_hub.security import safe_ai_get_doc, set_service_user_context
from wa_chat_hub.services import append_message


def send_patient_template(
    *,
    patient: str,
    template_name: str,
    language_code: str = "en",
    body_values: list[str] | None = None,
    event_key: str,
) -> dict[str, Any]:
    patient = cstr(patient).strip()
    template_name = cstr(template_name).strip()
    event_key = cstr(event_key).strip()
    if not patient:
        frappe.throw(_("Patient is required for an automated WhatsApp template."))
    if not template_name:
        frappe.throw(_("Template name is required for an automated WhatsApp template."))
    if not event_key:
        frappe.throw(_("Event key is required for an automated WhatsApp template."))

    original_user = frappe.session.user
    try:
        set_service_user_context(operation="shipment_whatsapp_template")
        patient_doc = safe_ai_get_doc("Patient", patient)
        route = get_or_create_mapped_patient_conversation(patient_doc)
        conversation = route["conversation"]
        channel_account = route["channel_account"]
        account = safe_ai_get_doc("Chat Channel Account", channel_account)
        if not account.is_active:
            frappe.throw(_("WhatsApp channel account {0} is disabled.").format(channel_account))
        if account.channel_type != "Interakt":
            frappe.throw(
                _("Automated shipment templates require an Interakt channel account; {0} uses {1}.").format(
                    channel_account,
                    account.channel_type,
                )
            )

        template = {
            "template_name": template_name,
            "language_code": cstr(language_code).strip() or "en",
            "body_values": [cstr(value) for value in (body_values or [])],
            "callback_data": event_key,
            "template_category": "UTILITY",
        }
        outbound = send_interakt_template_message(conversation, template)
        if not outbound.get("sent"):
            frappe.throw(_("Interakt did not confirm that the template was sent."))

        contact = safe_ai_get_doc("Chat Contact", route["contact"])
        provider_message_id = outbound.get("provider_message_id")
        message_result = append_message(
            {
                "channel_account": channel_account,
                "phone_number": contact.phone_number,
                "direction": "Outbound",
                "sender_type": "System",
                "content_type": "Template",
                "body": f"Template: {template_name}",
                "delivery_status": outbound.get("delivery_status") or "Sent",
                "channel_message_id": provider_message_id,
                "provider_message_id": provider_message_id,
                "provider_event_id": event_key,
                "provider_name": "Interakt",
                "dedupe_key": event_key,
                "raw_transport_payload": outbound,
                "template_category": template["template_category"],
            }
        )
        return {
            "conversation": conversation,
            "message": message_result.get("message"),
            "provider_message_id": provider_message_id,
            "delivery_status": outbound.get("delivery_status") or "Sent",
        }
    finally:
        frappe.set_user(original_user)
