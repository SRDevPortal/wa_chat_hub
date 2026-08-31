"""Authenticated-backend API for the native Flutter AI chat transport."""

from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.utils import cint, now_datetime

from wa_chat_hub.channel_resolver import (
    get_or_create_patient_conversation_for_channel_account,
)
from wa_chat_hub.phone_normalization import normalize_phone
from wa_chat_hub.policy import get_channel_policy
from wa_chat_hub.services import (
    append_message,
    find_indexed_phone_match_names,
    get_or_create_contact,
    get_or_create_conversation,
)


DEFAULT_MESSAGE_LIMIT = 100
MAX_MESSAGE_LIMIT = 200
DEFAULT_CHANNEL_ACCOUNT = "SRIAAS USA Patient App"


def get_mobile_app_setup_candidates() -> dict[str, Any]:
    """Read-only setup diagnostics for bench/administrators."""
    return {
        "channel_accounts": frappe.get_all(
            "Chat Channel Account",
            fields=["name", "channel_type", "is_active", "ai_policy_bundle"],
            order_by="modified desc",
        ),
        "policy_bundles": frappe.get_all(
            "WA AI Policy Bundle",
            fields=["name", "is_active"],
            order_by="modified desc",
        ),
        "agent_profiles": frappe.get_all(
            "WA AI Agent Profile",
            fields=["name", "agent_type", "is_active", "is_default"],
            order_by="modified desc",
        ),
    }


def ensure_mobile_app_channel_account(
    account_name: str = DEFAULT_CHANNEL_ACCOUNT,
    source_account: str | None = None,
) -> dict[str, Any]:
    """Create the app transport account while reusing an existing AI policy bundle."""
    account_name = str(account_name or DEFAULT_CHANNEL_ACCOUNT).strip()
    existing = frappe.db.exists("Chat Channel Account", account_name)
    if existing:
        account = frappe.get_doc("Chat Channel Account", existing)
        if account.channel_type != "Mobile App":
            frappe.throw(_("Channel account {0} already exists with another type.").format(account_name))
        if not account.is_active:
            account.is_active = 1
            account.save(ignore_permissions=True)
        return {"created": False, "channel_account": account.name}

    source_name = str(
        source_account or frappe.conf.get("mobile_app_ai_source_channel_account") or ""
    ).strip()
    if not source_name:
        source_name = frappe.db.get_value(
            "Chat Channel Account",
            {"is_active": 1, "channel_type": ["!=", "Mobile App"]},
            "name",
            order_by="modified desc",
        )
    source = frappe.get_doc("Chat Channel Account", source_name) if source_name else None
    policy_bundle = str(frappe.conf.get("mobile_app_ai_policy_bundle") or "").strip()
    if not policy_bundle and source:
        policy_bundle = str(source.ai_policy_bundle or "").strip()
    if not policy_bundle:
        frappe.throw(
            _("Configure mobile_app_ai_policy_bundle or select a source account with an AI Policy Bundle.")
        )

    values = {
        "doctype": "Chat Channel Account",
        "account_name": account_name,
        "channel_type": "Mobile App",
        "connector_status": "Active",
        "is_active": 1,
        "ai_policy_bundle": policy_bundle,
    }
    if source:
        for fieldname in (
            "department",
            "default_medical_department",
            "default_team",
            "interakt_default_country_code",
            "interakt_default_language_code",
        ):
            if source.get(fieldname):
                values[fieldname] = source.get(fieldname)
    account = frappe.get_doc(values).insert(ignore_permissions=True)
    frappe.db.commit()
    return {
        "created": True,
        "channel_account": account.name,
        "source_account": source.name if source else None,
        "ai_policy_bundle": policy_bundle,
    }


def _require_backend_token() -> None:
    """Accept only the existing backend-to-ERP shared secret."""
    expected = str(frappe.conf.get("mobile_app_erp_token") or "").strip()
    supplied = str(frappe.get_request_header("X-ERP-Token") or "").strip()
    if not supplied:
        auth = str(frappe.get_request_header("Authorization") or "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not expected:
        frappe.throw(
            _("Mobile app ERP token is not configured."),
            frappe.AuthenticationError,
        )
    if not supplied or not hmac.compare_digest(supplied, expected):
        frappe.throw(_("Invalid backend token."), frappe.AuthenticationError)


def _mobile_user(external_id: str):
    external_id = str(external_id or "").strip()
    if not external_id:
        frappe.throw(_("Mobile App User is required."))

    name = None
    if frappe.db.exists("Mobile App User", external_id):
        name = external_id
    else:
        for fieldname in ("external_id", "supabase_user_id"):
            name = frappe.db.get_value(
                "Mobile App User", {fieldname: external_id}, "name"
            )
            if name:
                break
    if not name:
        frappe.throw(_("Mobile App User was not found."), frappe.DoesNotExistError)

    doc = frappe.get_doc("Mobile App User", name)
    if not cint(doc.is_active):
        frappe.throw(_("Mobile App User is inactive."), frappe.PermissionError)
    return doc


def _mobile_channel_account() -> str:
    configured = str(
        frappe.conf.get("mobile_app_ai_channel_account") or ""
    ).strip()
    if configured:
        valid = frappe.db.get_value(
            "Chat Channel Account",
            {"name": configured, "channel_type": "Mobile App", "is_active": 1},
            "name",
        )
        if valid:
            return valid
        frappe.throw(
            _("Configured Mobile App AI channel account is missing or inactive.")
        )

    account = frappe.db.get_value(
        "Chat Channel Account",
        {"channel_type": "Mobile App", "is_active": 1},
        "name",
        order_by="modified desc",
    )
    if not account:
        frappe.throw(
            _("Create an active Chat Channel Account with Channel Type Mobile App.")
        )
    return account


def _profile_patient(user, profile_id: str | None = None) -> str | None:
    profiles = list(user.get("profiles") or [])
    requested = str(profile_id or "").strip()
    if requested:
        profiles = [
            row
            for row in profiles
            if requested
            in {
                str(row.name or "").strip(),
                str(row.profile_name or "").strip(),
                str(row.patient_id or "").strip(),
            }
        ]
        if not profiles:
            frappe.throw(_("The selected profile does not belong to this user."), frappe.PermissionError)

    patients = {
        str(row.patient_id).strip()
        for row in profiles
        if str(row.patient_id or "").strip()
        and frappe.db.exists("Patient", str(row.patient_id).strip())
    }
    if len(patients) == 1:
        return next(iter(patients))
    if len(patients) > 1:
        frappe.throw(_("Select a patient profile before starting AI chat."))
    return None


def _patient_from_phone(phone: str, channel_account: str) -> str | None:
    phone = normalize_phone(phone)
    if not phone:
        return None
    policy = get_channel_policy(channel_account)
    identity = policy.section("identity_policy") if policy else {}
    fields = [
        str(fieldname)
        for fieldname in ((identity.get("phone_fields") or {}).get("Patient") or [])
        if str(fieldname).strip()
    ]
    if not fields:
        return None
    matches = find_indexed_phone_match_names("Patient", fields, phone, limit=2)
    if len(matches) == 1:
        return next(iter(matches))
    return None


def _resolve_context(external_id: str, profile_id: str | None = None) -> dict[str, Any]:
    user = _mobile_user(external_id)
    account = _mobile_channel_account()
    phone = normalize_phone(user.phone)
    if not phone:
        frappe.throw(_("Add a verified mobile number to your app profile before using AI chat."))
    patient = _profile_patient(user, profile_id) or _patient_from_phone(phone, account)
    return {"user": user, "account": account, "phone": phone, "patient": patient}


def _ensure_conversation(context: dict[str, Any]) -> str:
    patient = context["patient"]
    if patient:
        result = get_or_create_patient_conversation_for_channel_account(
            frappe.get_doc("Patient", patient), context["account"]
        )
        conversation = result["conversation"]
        from wa_chat_hub.identity import reconcile_conversation_identity

        reconcile_conversation_identity(
            conversation,
            patient=patient,
            source="authenticated_mobile_app",
            verified=True,
        )
        frappe.db.set_value(
            "Chat Contact",
            result["contact"],
            {"linked_patient": patient, "source_doctype": "Patient", "source_name": patient},
            update_modified=False,
        )
        return conversation

    contact = get_or_create_contact(
        context["phone"], context["user"].full_name or context["phone"]
    )
    return get_or_create_conversation(context["account"], contact)


def _assert_conversation_owner(conversation: str, context: dict[str, Any]):
    convo = frappe.get_doc("Chat Conversation", str(conversation or "").strip())
    account = frappe.get_doc("Chat Channel Account", convo.channel_account)
    if account.channel_type != "Mobile App" or account.name != context["account"]:
        frappe.throw(_("Conversation was not found."), frappe.PermissionError)
    contact_phone = normalize_phone(
        frappe.db.get_value("Chat Contact", convo.contact, "phone_number")
    )
    if not contact_phone or contact_phone != context["phone"]:
        frappe.throw(_("Conversation was not found."), frappe.PermissionError)
    linked_patient = str(convo.linked_patient or "").strip()
    if linked_patient and linked_patient != str(context["patient"] or "").strip():
        frappe.throw(_("Conversation was not found."), frappe.PermissionError)
    return convo


def _message_rows(conversation: str, after: str | None = None, limit: int = DEFAULT_MESSAGE_LIMIT):
    filters: dict[str, Any] = {"conversation": conversation}
    after = str(after or "").strip()
    if after and frappe.db.exists("Chat Message", after):
        created = frappe.db.get_value("Chat Message", after, "creation")
        filters["creation"] = [">", created]
    rows = frappe.get_all(
        "Chat Message",
        filters=filters,
        fields=["name", "direction", "sender_type", "content_type", "body", "media_url", "delivery_status", "creation"],
        order_by="creation asc",
        limit_page_length=max(1, min(cint(limit or DEFAULT_MESSAGE_LIMIT), MAX_MESSAGE_LIMIT)),
    )
    return [
        {
            "id": str(row.name),
            "sender_type": row.sender_type or ("Customer" if row.direction == "Inbound" else "Agent"),
            "content_type": row.content_type or "Text",
            "message": row.body or "",
            "media_url": row.media_url or "",
            "delivery_status": row.delivery_status or "",
            "created_at": row.creation,
        }
        for row in rows
        if row.direction != "Internal Note"
    ]


def _session_payload(convo, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "conversation_id": str(convo.name),
        "status": convo.status,
        "identity_status": convo.identity_status or "Unverified",
        "patient": context["patient"],
        "profile_required": not bool(context["patient"]),
        "messages": _message_rows(str(convo.name)),
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def open_session(external_id: str, profile_id: str | None = None):
    _require_backend_token()
    context = _resolve_context(external_id, profile_id)
    conversation = _ensure_conversation(context)
    convo = _assert_conversation_owner(conversation, context)
    frappe.db.commit()
    return {"success": True, "data": _session_payload(convo, context)}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def send_message(
    external_id: str,
    conversation: str,
    message: str,
    client_message_id: str,
    profile_id: str | None = None,
):
    _require_backend_token()
    context = _resolve_context(external_id, profile_id)
    convo = _assert_conversation_owner(conversation, context)
    text = str(message or "").strip()
    if not text:
        frappe.throw(_("Message is required."))
    if len(text) > 4000:
        frappe.throw(_("Message cannot exceed 4000 characters."))
    client_message_id = str(client_message_id or "").strip()
    if not client_message_id:
        frappe.throw(_("Client message ID is required."))

    result = append_message(
        {
            "channel_account": convo.channel_account,
            "phone_number": context["phone"],
            "display_name": context["user"].full_name,
            "direction": "Inbound",
            "sender_type": "Customer",
            "content_type": "Text",
            "body": text,
            "provider_name": "Mobile App",
            "provider_message_id": client_message_id,
            "delivery_status": "Received",
        }
    )
    return {
        "success": True,
        "data": {
            "conversation_id": str(convo.name),
            "message_id": str(result["message"]),
            "duplicate": bool(result.get("duplicate")),
        },
    }


@frappe.whitelist(allow_guest=True, methods=["POST"])
def send_attachment(
    external_id: str,
    conversation: str,
    content_type: str,
    media_url: str,
    file_name: str,
    client_message_id: str,
    caption: str | None = None,
    profile_id: str | None = None,
):
    _require_backend_token()
    context = _resolve_context(external_id, profile_id)
    convo = _assert_conversation_owner(conversation, context)
    content_type = str(content_type or "").title()
    if content_type not in {"Image", "Video", "Audio", "Document"}:
        frappe.throw(_("Unsupported attachment type."))
    media_url = str(media_url or "").strip()
    parsed = urlparse(media_url)
    if parsed.scheme != "https" or not parsed.netloc or len(media_url) > 2000:
        frappe.throw(_("A valid secure attachment URL is required."))
    file_name = str(file_name or "Attachment").strip()[:240]
    client_message_id = str(client_message_id or "").strip()
    if not client_message_id:
        frappe.throw(_("Client message ID is required."))
    body = str(caption or "").strip()[:1000] or file_name

    result = append_message(
        {
            "channel_account": convo.channel_account,
            "phone_number": context["phone"],
            "display_name": context["user"].full_name,
            "direction": "Inbound",
            "sender_type": "Customer",
            "content_type": content_type,
            "body": body,
            "media_url": media_url,
            "provider_name": "Mobile App",
            "provider_message_id": client_message_id,
            "delivery_status": "Received",
        }
    )
    return {
        "success": True,
        "data": {
            "conversation_id": str(convo.name),
            "message_id": str(result["message"]),
            "media_url": media_url,
            "duplicate": bool(result.get("duplicate")),
        },
    }


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_messages(
    external_id: str,
    conversation: str,
    profile_id: str | None = None,
    after: str | None = None,
    limit: int = DEFAULT_MESSAGE_LIMIT,
):
    _require_backend_token()
    context = _resolve_context(external_id, profile_id)
    convo = _assert_conversation_owner(conversation, context)
    return {
        "success": True,
        "data": {
            "conversation_id": str(convo.name),
            "status": convo.status,
            "messages": _message_rows(str(convo.name), after=after, limit=limit),
        },
    }

