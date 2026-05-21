from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.desk.form import assign_to
from frappe.utils import cint, now_datetime

from wa_chat_hub.messaging.attribution import get_conversation_attribution
from wa_chat_hub.messaging.windows import get_messaging_window_state
from wa_chat_hub.services import append_message, build_erp_actions, mark_conversation_read
from wa_chat_hub.services import normalize_phone


@frappe.whitelist(methods=["POST"])
def ingest_message():
    payload = frappe.local.form_dict or {}
    if frappe.request and frappe.request.get_json(silent=True):
        payload = frappe.request.get_json()

    required = ["channel_account"]
    missing = [field for field in required if not payload.get(field)]
    if missing:
        frappe.throw(_("Missing required fields: {0}").format(", ".join(missing)))

    result = append_message(payload)
    return {"success": True, "result": result}


@frappe.whitelist()
def get_channel_accounts():
    rows = frappe.get_all(
        "Chat Channel Account",
        filters={"is_active": 1},
        fields=["name", "account_name", "channel_type", "phone_number", "connector_status"],
        order_by="account_name asc",
    )
    return {"success": True, "result": rows}


@frappe.whitelist()
def get_conversations(limit=50, status=None, assigned_to=None, department=None, channel_account=None):
    filters = {}
    if status:
        filters["status"] = status
    if assigned_to:
        filters["assigned_to"] = assigned_to
    if department:
        filters["department"] = department
    if channel_account and str(channel_account).strip().lower() not in {"", "all", "__all__"}:
        filters["channel_account"] = channel_account

    rows = frappe.get_all(
        "Chat Conversation",
        filters=filters,
        fields=[
            "name",
            "channel_account",
            "contact",
            "department",
            "assigned_to",
            "status",
            "priority",
            "lead_score",
            "lead_lan",
            "lead_temperature",
            "last_message_preview",
            "unread_count",
            "modified",
        ],
        order_by="modified desc",
        limit_page_length=int(limit),
    )

    contact_names = [row.contact for row in rows if row.contact]
    contacts = {}
    if contact_names:
        for c in frappe.get_all("Chat Contact", filters={"name": ["in", contact_names]}, fields=["name", "display_name", "phone_number"]):
            contacts[c.name] = c

    return {
        "success": True,
        "result": [
            {
                **row,
                "contact_display_name": contacts.get(row.contact, {}).get("display_name"),
                "contact_phone_number": contacts.get(row.contact, {}).get("phone_number"),
            }
            for row in rows
        ],
    }


@frappe.whitelist()
def get_messages(conversation, limit=100):
    rows = frappe.get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=[
            "name",
            "direction",
            "sender_type",
            "content_type",
            "body",
            "media_url",
            "attachment_file",
            "channel_message_id",
            "delivery_status",
            "raw_transport_payload",
            "creation",
        ],
        order_by="creation asc",
        limit_page_length=int(limit),
    )
    return {"success": True, "result": rows}


@frappe.whitelist(methods=["POST"])
def mark_read(conversation):
    mark_conversation_read(conversation)
    return {"success": True}


@frappe.whitelist(methods=["POST"])
def bulk_assign(conversations, user=None):
    names = _as_list(conversations)
    if not names:
        frappe.throw(_("Select at least one conversation"))

    for name in names:
        _ensure_conversation_write(name)
        frappe.db.set_value("Chat Conversation", name, "assigned_to", user or None)
        assign_to.clear("Chat Conversation", name)
        if user:
            assign_to.add(
                {
                    "assign_to": [user],
                    "doctype": "Chat Conversation",
                    "name": name,
                    "description": _("WhatsApp conversation assigned"),
                    "notify": 0,
                },
                ignore_permissions=True,
            )

    frappe.db.commit()
    return {"success": True, "updated": len(names)}


@frappe.whitelist(methods=["POST"])
def bulk_update(conversations, fieldname, value):
    if fieldname not in {"status", "priority"}:
        frappe.throw(_("Invalid field for bulk update"))

    valid_values = {
        "status": {"Open", "Pending", "Resolved", "Closed"},
        "priority": {"Low", "Medium", "High", "Urgent"},
    }
    if value not in valid_values[fieldname]:
        frappe.throw(_("Invalid value for {0}").format(fieldname))

    names = _as_list(conversations)
    if not names:
        frappe.throw(_("Select at least one conversation"))

    for name in names:
        _ensure_conversation_write(name)
        frappe.db.set_value("Chat Conversation", name, fieldname, value)

    frappe.db.commit()
    return {"success": True, "updated": len(names)}


@frappe.whitelist(methods=["POST"])
def add_external_outbound_message(conversation, body, delivery_status="Sent", channel_message_id=None):
    if not conversation:
        frappe.throw(_("conversation is required"))
    if not body:
        frappe.throw(_("body is required"))
    _ensure_conversation_write(conversation)

    convo = frappe.get_doc("Chat Conversation", conversation)
    if channel_message_id and frappe.db.exists("Chat Message", {"channel_message_id": channel_message_id}):
        return {"success": True, "message": "Duplicate message ignored"}

    result = append_message({
        "channel_account": convo.channel_account,
        "phone_number": frappe.db.get_value("Chat Contact", convo.contact, "phone_number"),
        "direction": "Outbound",
        "sender_type": "Agent",
        "content_type": "Text",
        "body": body,
        "delivery_status": delivery_status or "Sent",
        "channel_message_id": channel_message_id,
        "raw_payload": {"source": "manual_interakt_sync"},
    })
    return {"success": True, "result": result}


@frappe.whitelist()
def get_sidebar_context(conversation):
    from wa_chat_hub.messaging.windows import _ensure_messaging_window_schema

    _ensure_messaging_window_schema()
    convo = frappe.get_doc("Chat Conversation", conversation)
    try:
        convo.reload()
    except Exception:
        pass
    contact = frappe.get_doc("Chat Contact", convo.contact)
    actions = build_erp_actions()
    messaging_window = get_messaging_window_state(conversation, convo=convo)
    attribution = get_conversation_attribution(conversation)
    persisted_attribution = tuple(
        getattr(convo, key, None) for key in ("source_id", "source_url", "source", "ctwa_clid")
    )
    if not any(persisted_attribution):
        for key in ("source_id", "source_url", "source", "ctwa_clid"):
            if messaging_window.get(key):
                attribution[key] = messaging_window[key]

    return {
        "success": True,
        "result": {
            "conversation": convo.as_dict(),
            "contact": contact.as_dict(),
            "attribution": attribution,
            "messaging_window": messaging_window,
            "actions": actions,
            "server_time": str(now_datetime()),
        },
    }


@frappe.whitelist()
def get_messaging_window(conversation):
    if not conversation:
        frappe.throw(_("conversation is required"))
    return {"success": True, "result": get_messaging_window_state(conversation)}


def _conversation_for_reference(reference_doctype: str, reference_name: str | None) -> str | None:
    if not reference_doctype or not reference_name:
        return None

    if reference_doctype == "CRM Lead" and frappe.get_meta("Chat Conversation").has_field(
        "linked_crm_lead"
    ):
        conv = frappe.db.get_value(
            "Chat Conversation",
            {"linked_crm_lead": reference_name},
            "name",
        )
        if conv:
            return conv

    if reference_doctype == "Patient Encounter":
        patient = frappe.db.get_value("Patient Encounter", reference_name, "patient")
        if patient:
            conv = _conversation_for_reference("Patient", patient)
            if conv:
                return conv

    return frappe.db.get_value(
        "Chat Conversation",
        {
            "linked_reference_doctype": reference_doctype,
            "linked_reference_name": reference_name,
        },
        "name",
    )


@frappe.whitelist()
def get_conversation_for_reference(reference_doctype, reference_name):
    if not reference_doctype or not reference_name:
        frappe.throw(_("reference_doctype and reference_name are required"))

    conversation = _conversation_for_reference(reference_doctype, reference_name)
    if conversation:
        return {"success": True, "conversation": conversation}

    created = _try_create_conversation_for_reference(reference_doctype, reference_name)
    if created:
        return {"success": True, "conversation": created, "created": True}

    message = _("No WhatsApp conversation found for this record.")
    if reference_doctype in ("Patient", "Patient Encounter", "CRM Lead"):
        doc = _load_reference_doc(reference_doctype, reference_name)
        if doc and not _reference_has_phone(doc):
            message = _("Add a mobile number on this record to open WhatsApp chat.")

    return {
        "success": False,
        "conversation": None,
        "message": message,
    }


def _load_reference_doc(reference_doctype: str, reference_name: str):
    if reference_doctype == "Patient Encounter":
        patient = frappe.db.get_value("Patient Encounter", reference_name, "patient")
        if patient and frappe.db.exists("Patient", patient):
            return frappe.get_doc("Patient", patient)
        return None
    if frappe.db.exists(reference_doctype, reference_name):
        return frappe.get_doc(reference_doctype, reference_name)
    return None


def _try_create_conversation_for_reference(reference_doctype: str, reference_name: str) -> str | None:
    try:
        if reference_doctype == "CRM Lead" and frappe.db.exists("CRM Lead", reference_name):
            from wa_chat_hub.channel_resolver import get_or_create_mapped_lead_conversation

            lead = frappe.get_doc("CRM Lead", reference_name)
            if not _reference_has_phone(lead):
                return None
            return get_or_create_mapped_lead_conversation(lead)["conversation"]

        patient_name = reference_name
        if reference_doctype == "Patient Encounter":
            patient_name = frappe.db.get_value("Patient Encounter", reference_name, "patient")
            if not patient_name:
                return None

        if reference_doctype in ("Patient", "Patient Encounter") and patient_name and frappe.db.exists(
            "Patient", patient_name
        ):
            from wa_chat_hub.channel_resolver import get_or_create_mapped_patient_conversation

            patient = frappe.get_doc("Patient", patient_name)
            if not _reference_has_phone(patient):
                return None
            return get_or_create_mapped_patient_conversation(patient)["conversation"]
    except frappe.ValidationError:
        raise
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Create Conversation For Reference Failed")
    return None


def _reference_has_phone(doc) -> bool:
    from wa_chat_hub.services import normalize_phone

    meta = frappe.get_meta(doc.doctype)
    for fieldname in (
        "mobile",
        "mobile_no",
        "phone",
        "whatsapp_number",
        "whatsapp_no",
        "custom_whatsapp_number",
    ):
        if meta.has_field(fieldname) and doc.get(fieldname):
            if normalize_phone(doc.get(fieldname)):
                return True
    return False


@frappe.whitelist()
def get_reference_chat_statuses(reference_doctype, reference_names=None):
    if not reference_doctype:
        frappe.throw(_("reference_doctype is required"))

    names = _parse_reference_names(reference_names)
    result = {
        name: {"unread_count": 0, "conversation_count": 0, "last_message_time": ""}
        for name in names
    }
    if not names:
        return {"success": True, "result": result}

    conv_rows: dict[str, list[dict]] = {name: [] for name in names}
    meta = frappe.get_meta("Chat Conversation")

    if reference_doctype == "CRM Lead" and meta.has_field("linked_crm_lead"):
        for row in frappe.get_all(
            "Chat Conversation",
            filters={"linked_crm_lead": ["in", names]},
            fields=["name", "linked_crm_lead", "unread_count", "modified"],
        ):
            conv_rows.setdefault(row.linked_crm_lead, []).append(row)

    for row in frappe.get_all(
        "Chat Conversation",
        filters={
            "linked_reference_doctype": reference_doctype,
            "linked_reference_name": ["in", names],
        },
        fields=["name", "linked_reference_name", "unread_count", "modified"],
    ):
        conv_rows.setdefault(row.linked_reference_name, []).append(row)

    if reference_doctype == "Patient Encounter":
        for enc in frappe.get_all(
            "Patient Encounter",
            filters={"name": ["in", names]},
            fields=["name", "patient"],
        ):
            if not enc.patient:
                continue
            patient_conv = _conversation_for_reference("Patient", enc.patient)
            if not patient_conv:
                continue
            stats = frappe.db.get_value(
                "Chat Conversation",
                patient_conv,
                ["unread_count", "modified"],
                as_dict=True,
            )
            if stats:
                conv_rows.setdefault(enc.name, []).append(
                    {
                        "name": patient_conv,
                        "unread_count": stats.unread_count,
                        "modified": stats.modified,
                    }
                )

    for name in names:
        rows = conv_rows.get(name) or []
        if not rows:
            continue
        result[name] = {
            "unread_count": sum(cint(row.get("unread_count")) for row in rows),
            "conversation_count": len(rows),
            "last_message_time": max((row.get("modified") or "") for row in rows),
        }

    return {"success": True, "result": result}


@frappe.whitelist()
def resolve_chat_for_reference(reference_doctype, reference_name=None, phone_number=None):
    if not reference_doctype:
        frappe.throw(_("reference_doctype is required"))

    if reference_name:
        conv = _conversation_for_reference(reference_doctype, reference_name)
        if conv:
            return {"success": True, "result": {"conversation": conv}}

    normalized = normalize_phone(phone_number)
    if normalized:
        contact = frappe.db.get_value("Chat Contact", {"phone_number": normalized}, "name")
        if contact:
            conv = frappe.db.get_value("Chat Conversation", {"contact": contact, "status": ["!=", "Closed"]}, "name")
            if conv:
                return {"success": True, "result": {"conversation": conv}}

    return {"success": True, "result": {"conversation": None}}


def _as_list(value):
    if isinstance(value, str):
        try:
            value = frappe.parse_json(value)
        except Exception:
            value = [value]
    if not isinstance(value, list):
        return []
    return [item.get("name") if isinstance(item, dict) else item for item in value if item]


def _parse_reference_names(reference_names) -> list[str]:
    if not reference_names:
        return []
    if isinstance(reference_names, str):
        try:
            reference_names = json.loads(reference_names)
        except Exception:
            reference_names = [reference_names]
    if not isinstance(reference_names, list):
        return []
    return [str(name) for name in reference_names if name]


def _ensure_conversation_write(name):
    if not frappe.has_permission("Chat Conversation", "write", name):
        frappe.throw(_("Not permitted to update conversation {0}").format(name), frappe.PermissionError)
