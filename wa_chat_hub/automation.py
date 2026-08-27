from __future__ import annotations

from typing import Any

import frappe
from frappe import _
from frappe.utils import cstr

from wa_chat_hub.channel_resolver import get_or_create_patient_conversation_for_channel_account
from wa_chat_hub.delivery_outcomes import (
    PatientTemplateDeliveryError,
    PatientTemplateNotSentError,
    PatientTemplateOutcomeUnknownError,
)
from wa_chat_hub.interakt.templates_api import resolve_approved_template
from wa_chat_hub.messaging.channel_map import resolve_patient_department_map
from wa_chat_hub.outbound import send_interakt_template_message
from wa_chat_hub.security import safe_ai_get_all, safe_ai_get_doc, set_service_user_context
from wa_chat_hub.services import append_message


ROUTABLE_CONVERSATION_STATUSES = ("Open", "Pending", "Resolved")


def send_patient_template(
    *,
    patient: str,
    template_name: str,
    language_code: str = "en",
    body_values: list[str] | None = None,
    body_preview: str | None = None,
    event_key: str,
    fallback_channel_account: str | None = None,
) -> dict[str, Any]:
    patient = cstr(patient).strip()
    template_name = cstr(template_name).strip()
    event_key = cstr(event_key).strip()
    if not patient:
        raise PatientTemplateNotSentError(_("Patient is required for an automated WhatsApp template."))
    if not template_name:
        raise PatientTemplateNotSentError(_("Template name is required for an automated WhatsApp template."))
    if not event_key:
        raise PatientTemplateNotSentError(_("Event key is required for an automated WhatsApp template."))

    original_user = frappe.session.user
    try:
        set_service_user_context(operation="patient_notification_template")
        try:
            patient_doc = safe_ai_get_doc("Patient", patient)
            route = resolve_patient_route(
                patient_doc,
                fallback_channel_account=cstr(fallback_channel_account).strip() or None,
            )
            frappe.db.commit()
            conversation = route["conversation"]
            channel_account = route["channel_account"]
            account = safe_ai_get_doc("Chat Channel Account", channel_account)
            if not account.is_active:
                raise PatientTemplateNotSentError(
                    _("WhatsApp channel account {0} is disabled.").format(channel_account)
                )
            if account.channel_type != "Interakt":
                raise PatientTemplateNotSentError(
                    _("Automated patient templates require an Interakt channel account; {0} uses {1}.").format(
                        channel_account,
                        account.channel_type,
                    )
                )

            template = {
                "template_name": template_name,
                "language_code": cstr(language_code).strip() or "en",
                "body_values": [cstr(value) for value in (body_values or [])],
                "body_preview": cstr(body_preview).strip(),
                "callback_data": event_key,
                "template_category": "UTILITY",
            }
            template = resolve_approved_template(channel_account, template)
        except PatientTemplateDeliveryError:
            raise
        except Exception as exc:
            raise PatientTemplateNotSentError(cstr(exc), retryable=False) from exc

        try:
            outbound = send_interakt_template_message(conversation, template)
        except PatientTemplateDeliveryError:
            raise
        except Exception as exc:
            raise PatientTemplateOutcomeUnknownError(cstr(exc)) from exc
        if not outbound.get("sent"):
            raise PatientTemplateNotSentError(
                _("Interakt did not confirm that the template was sent."),
                retryable=False,
            )

        try:
            contact = safe_ai_get_doc("Chat Contact", route["contact"])
            provider_message_id = outbound.get("provider_message_id")
            message_result = append_message(
                {
                    "channel_account": channel_account,
                    "phone_number": contact.phone_number,
                    "direction": "Outbound",
                    "sender_type": "System",
                    "content_type": "Template",
                    "body": template.get("body_preview") or f"Template: {template['template_name']}",
                    "delivery_status": outbound.get("delivery_status") or "Sent",
                    "channel_message_id": provider_message_id,
                    "provider_message_id": provider_message_id,
                    "provider_event_id": event_key,
                    "provider_name": "Interakt",
                    "dedupe_key": event_key,
                    "raw_transport_payload": {
                        **outbound,
                        "template_name": template.get("template_name"),
                        "configured_template_name": template.get("configured_template_name"),
                        "language_code": template.get("language_code"),
                        "body_values": template.get("body_values"),
                        "body_preview": template.get("body_preview"),
                    },
                    "template_category": template["template_category"],
                }
            )
        except Exception as exc:
            raise PatientTemplateOutcomeUnknownError(cstr(exc)) from exc
        return {
            "conversation": conversation,
            "message": message_result.get("message"),
            "provider_message_id": provider_message_id,
            "delivery_status": outbound.get("delivery_status") or "Sent",
            "channel_account": channel_account,
            "routing_source": route["routing_source"],
        }
    finally:
        frappe.set_user(original_user)

def resolve_patient_route(patient_doc, fallback_channel_account: str | None = None) -> dict[str, Any]:
    frappe.throw(_("Patient notification routing is disabled for ShipKia customer flow."))

    existing = find_existing_patient_route(patient_doc.name)
    if existing:
        return existing

    pipeline_row = resolve_patient_department_map(patient_doc)
    if pipeline_row:
        route = get_or_create_patient_conversation_for_channel_account(
            patient_doc,
            pipeline_row["chat_channel_account"],
            pipeline_map=pipeline_row.get("name"),
        )
        route["routing_source"] = (
            "Department Default" if pipeline_row.get("is_department_default") else "Department Map"
        )
        return route

    if fallback_channel_account:
        route = get_or_create_patient_conversation_for_channel_account(
            patient_doc,
            fallback_channel_account,
        )
        route["routing_source"] = "Patient Notification Settings Fallback"
        return route

    frappe.throw(
        _(
            "No department route or Patient Notification fallback is available for Patient {0}."
        ).format(patient_doc.name)
    )


def find_existing_patient_route(patient: str) -> dict[str, Any] | None:
    return None

    filters_list = []
    conversation_meta = frappe.get_meta("Chat Conversation")
    if conversation_meta.has_field("linked_patient"):
        filters_list.append(
            {"linked_patient": patient, "status": ["in", ROUTABLE_CONVERSATION_STATUSES]}
        )
    if conversation_meta.has_field("linked_reference_doctype"):
        filters_list.append(
            {
                "linked_reference_doctype": "Patient",
                "linked_reference_name": patient,
                "status": ["in", ROUTABLE_CONVERSATION_STATUSES],
            }
        )

    for filters in filters_list:
        rows = safe_ai_get_all(
            "Chat Conversation",
            filters=filters,
            fields=["name", "channel_account", "contact"],
            order_by="modified desc",
            limit_start=0,
            limit_page_length=1,
        )
        if not rows:
            continue
        row = rows[0]
        account = safe_ai_get_doc("Chat Channel Account", row.channel_account)
        if not account.is_active or account.channel_type != "Interakt":
            continue
        return {
            "conversation": row.name,
            "channel_account": row.channel_account,
            "contact": row.contact,
            "created": False,
            "pipeline_map": None,
            "routing_source": "Existing Conversation",
        }
    return None
