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

CHAT_HUB_SCOPE_DOCTYPES = {"CRM Lead", "Lead", "Patient", "Patient Encounter"}


def _chat_hub_scope_key(user: str | None = None) -> str:
    return f"wa_chat_hub:scope:{user or frappe.session.user}"


def _normalize_scope_doctype(reference_doctype: str | None) -> str | None:
    reference_doctype = (reference_doctype or "").strip()
    if reference_doctype in CHAT_HUB_SCOPE_DOCTYPES:
        return reference_doctype
    return None


def _get_current_chat_hub_scope() -> dict:
    try:
        scope = frappe.cache().get_value(_chat_hub_scope_key()) or {}
    except Exception:
        scope = {}
    if not isinstance(scope, dict):
        scope = {}

    reference_doctype = _normalize_scope_doctype(scope.get("reference_doctype"))
    return {
        "reference_doctype": reference_doctype,
        "locked": bool(cint(scope.get("locked")) and reference_doctype),
    }


def _force_scoped_reference_doctype(reference_doctype: str | None = None) -> str | None:
    scope = _get_current_chat_hub_scope()
    if scope.get("locked") and scope.get("reference_doctype"):
        return scope["reference_doctype"]
    return reference_doctype


@frappe.whitelist(methods=["POST"])
def set_chat_hub_scope(reference_doctype, locked=1):
    reference_doctype = _normalize_scope_doctype(reference_doctype)
    if not reference_doctype:
        frappe.throw(_("Invalid WA Chat Hub scope"))

    scope = {
        "reference_doctype": reference_doctype,
        "locked": 1 if cint(locked) else 0,
    }
    frappe.cache().set_value(_chat_hub_scope_key(), scope)
    return {"success": True, "scope": scope}


@frappe.whitelist()
def get_chat_hub_scope():
    return {"success": True, "scope": _get_current_chat_hub_scope()}


@frappe.whitelist(methods=["POST"])
def clear_chat_hub_scope():
    frappe.cache().delete_value(_chat_hub_scope_key())
    return {"success": True, "scope": {"reference_doctype": None, "locked": False}}


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


CONVERSATION_LIST_FIELDS = [
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
    "linked_crm_lead",
    "linked_reference_doctype",
    "linked_reference_name",
    "modified",
]

MAX_REFERENCE_STATUS_NAMES = 500


def _conversation_list_filters(
    *,
    status=None,
    assigned_to=None,
    department=None,
    channel_account=None,
    reference_doctype=None,
) -> dict:
    filters: dict = {}
    if status:
        filters["status"] = status
    if assigned_to:
        filters["assigned_to"] = assigned_to
    if department:
        filters["department"] = department
    if channel_account and str(channel_account).strip().lower() not in {"", "all", "__all__"}:
        filters["channel_account"] = channel_account
    if reference_doctype and frappe.get_meta("Chat Conversation").has_field("linked_reference_doctype"):
        reference_doctype = str(reference_doctype).strip()
        if reference_doctype == "Patient":
            filters["linked_reference_doctype"] = ["in", ["Patient", "Patient Encounter"]]
        elif reference_doctype == "CRM Lead":
            filters["linked_reference_doctype"] = ["in", ["CRM Lead", "Lead"]]
        else:
            filters["linked_reference_doctype"] = reference_doctype
    return filters


def _enrich_conversation_rows(rows: list) -> list:
    contact_names = []
    conversation_names = []
    for row in rows:
        data = row if isinstance(row, dict) else row.as_dict()
        if data.get("name"):
            conversation_names.append(data["name"])
        if data.get("contact"):
            contact_names.append(data["contact"])

    last_message_times = _conversation_last_message_times(conversation_names)

    contacts: dict = {}
    if contact_names:
        for contact in frappe.get_all(
            "Chat Contact",
            filters={"name": ["in", contact_names]},
            fields=["name", "display_name", "phone_number"],
        ):
            contacts[contact.name] = contact

    enriched = []
    for row in rows:
        data = row if isinstance(row, dict) else row.as_dict()
        contact = contacts.get(data.get("contact"), {})
        enriched.append(
            {
                **data,
                "contact_display_name": contact.get("display_name"),
                "contact_phone_number": contact.get("phone_number"),
                "last_message_time": last_message_times.get(data.get("name")) or data.get("modified"),
            }
        )
    return sorted(enriched, key=lambda row: str(row.get("last_message_time") or ""), reverse=True)


def _conversation_last_message_times(conversation_names: list[str]) -> dict[str, str]:
    if not conversation_names:
        return {}

    times = {}
    for row in frappe.get_all(
        "Chat Message",
        filters={"conversation": ["in", conversation_names]},
        fields=["conversation", "creation"],
        order_by="creation desc",
        limit_page_length=0,
    ):
        if row.conversation not in times:
            times[row.conversation] = row.creation
    return times


def _matching_contact_names(query: str) -> list[str]:
    q_like = f"%{query}%"
    or_filters = [
        ["display_name", "like", q_like],
        ["phone_number", "like", q_like],
        ["name", "like", q_like],
    ]
    phone = normalize_phone(query)
    if phone and len(phone) >= 4:
        last10 = phone[-10:]
        or_filters.append(["phone_number", "like", f"%{last10}%"])
    return frappe.get_all("Chat Contact", or_filters=or_filters, pluck="name", limit_page_length=200)


def _matching_reference_conversation_names(query: str, base_filters: dict) -> set[str]:
    names: set[str] = set()
    q_like = f"%{query}%"

    if frappe.db.exists("DocType", "CRM Lead"):
        lead_meta = frappe.get_meta("CRM Lead")
        lead_or = [["lead_name", "like", q_like], ["name", "like", q_like]]
        for fieldname in ("mobile_no", "phone", "mobile", "email"):
            if lead_meta.has_field(fieldname):
                lead_or.append([fieldname, "like", q_like])
        lead_names = frappe.get_all("CRM Lead", or_filters=lead_or, pluck="name", limit_page_length=100)
        if lead_names:
            conv_meta = frappe.get_meta("Chat Conversation")
            if conv_meta.has_field("linked_crm_lead"):
                for row in frappe.get_all(
                    "Chat Conversation",
                    filters={**base_filters, "linked_crm_lead": ["in", lead_names]},
                    pluck="name",
                    limit_page_length=200,
                ):
                    names.add(row)
            for row in frappe.get_all(
                "Chat Conversation",
                filters={
                    **base_filters,
                    "linked_reference_doctype": "CRM Lead",
                    "linked_reference_name": ["in", lead_names],
                },
                pluck="name",
                limit_page_length=200,
            ):
                names.add(row)

    if frappe.db.exists("DocType", "Patient"):
        patient_meta = frappe.get_meta("Patient")
        patient_or = [["patient_name", "like", q_like], ["name", "like", q_like]]
        if patient_meta.has_field("sr_patient_id"):
            patient_or.append(["sr_patient_id", "like", q_like])
        for fieldname in ("mobile", "mobile_no", "phone"):
            if patient_meta.has_field(fieldname):
                patient_or.append([fieldname, "like", q_like])
        patient_names = frappe.get_all("Patient", or_filters=patient_or, pluck="name", limit_page_length=100)
        if patient_names:
            for row in frappe.get_all(
                "Chat Conversation",
                filters={
                    **base_filters,
                    "linked_reference_doctype": "Patient",
                    "linked_reference_name": ["in", patient_names],
                },
                pluck="name",
                limit_page_length=200,
            ):
                names.add(row)

    return names


@frappe.whitelist()
def get_conversations(
    limit=50,
    status=None,
    assigned_to=None,
    department=None,
    channel_account=None,
    reference_doctype=None,
):
    reference_doctype = _force_scoped_reference_doctype(reference_doctype)
    filters = _conversation_list_filters(
        status=status,
        assigned_to=assigned_to,
        department=department,
        channel_account=channel_account,
        reference_doctype=reference_doctype,
    )

    rows = frappe.get_all(
        "Chat Conversation",
        filters=filters,
        fields=CONVERSATION_LIST_FIELDS,
        order_by="modified desc",
        limit_page_length=int(limit),
    )

    return {"success": True, "result": _enrich_conversation_rows(rows)}


@frappe.whitelist()
def search_conversations(
    query,
    limit=100,
    status=None,
    assigned_to=None,
    department=None,
    channel_account=None,
    reference_doctype=None,
):
    reference_doctype = _force_scoped_reference_doctype(reference_doctype)
    """Search conversations by phone, name, lead, patient, or message preview."""
    q = (query or "").strip()
    if not q:
        return get_conversations(
            limit=limit,
            status=status,
            assigned_to=assigned_to,
            department=department,
            channel_account=channel_account,
            reference_doctype=reference_doctype,
        )

    base_filters = _conversation_list_filters(
        status=status,
        assigned_to=assigned_to,
        department=department,
        channel_account=channel_account,
        reference_doctype=reference_doctype,
    )
    q_like = f"%{q}%"
    matching: set[str] = set()

    contact_names = _matching_contact_names(q)
    if contact_names:
        for name in frappe.get_all(
            "Chat Conversation",
            filters={**base_filters, "contact": ["in", contact_names]},
            pluck="name",
            limit_page_length=int(limit),
        ):
            matching.add(name)

    conv_or_filters = [
        ["name", "like", q_like],
        ["linked_reference_name", "like", q_like],
        ["last_message_preview", "like", q_like],
        ["channel_account", "like", q_like],
    ]
    if frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
        conv_or_filters.append(["linked_crm_lead", "like", q_like])

    for row in frappe.get_all(
        "Chat Conversation",
        filters=base_filters,
        or_filters=conv_or_filters,
        fields=["name"],
        limit_page_length=int(limit),
    ):
        matching.add(row.name)

    matching.update(_matching_reference_conversation_names(q, base_filters))

    if not matching:
        return {"success": True, "result": []}

    rows = frappe.get_all(
        "Chat Conversation",
        filters={"name": ["in", list(matching)]},
        fields=CONVERSATION_LIST_FIELDS,
        order_by="modified desc",
        limit_page_length=int(limit),
    )
    return {"success": True, "result": _enrich_conversation_rows(rows)}


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


def _conversation_for_reference(
    reference_doctype: str,
    reference_name: str | None,
    *,
    include_crm_lead_aliases: bool = False,
) -> str | None:
    if not reference_doctype or not reference_name:
        return None

    reference_names = [reference_name]
    if reference_doctype == "CRM Lead" and include_crm_lead_aliases:
        reference_names = _crm_lead_reference_names(reference_name)

    if reference_doctype == "CRM Lead" and frappe.get_meta("Chat Conversation").has_field(
        "linked_crm_lead"
    ):
        conv = frappe.db.get_value(
            "Chat Conversation",
            {"linked_crm_lead": ["in", reference_names]},
            "name",
            order_by="modified desc",
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
            "linked_reference_name": ["in", reference_names],
        },
        "name",
        order_by="modified desc",
    )


def _crm_lead_reference_names(reference_name: str) -> list[str]:
    names = []
    primary = _resolve_primary_crm_lead(reference_name)
    for name in (primary, reference_name):
        if name and name not in names:
            names.append(name)

    if names and frappe.db.has_column("CRM Lead", "sr_duplicate_of_name"):
        for duplicate in frappe.get_all(
            "CRM Lead",
            filters={"sr_duplicate_of_name": ["in", names]},
            pluck="name",
            limit_page_length=0,
        ):
            if duplicate not in names:
                names.append(duplicate)
    return names or [reference_name]


def _resolve_primary_crm_lead(lead_name: str | None) -> str | None:
    if not lead_name or not frappe.db.exists("CRM Lead", lead_name):
        return None
    try:
        from crm_lead_dedupe.leads.dup_utils import get_primary_lead_name_for_lead

        return get_primary_lead_name_for_lead(lead_name) or lead_name
    except Exception:
        pass

    if frappe.db.has_column("CRM Lead", "sr_duplicate_of_name"):
        primary = frappe.db.get_value("CRM Lead", lead_name, "sr_duplicate_of_name")
        if primary and frappe.db.exists("CRM Lead", primary):
            return primary
    return lead_name


def _crm_lead_reference_lookup(reference_names: list[str]) -> dict[str, str]:
    """Map CRM Lead aliases to the requested lead name in one batch."""
    names = [name for name in reference_names if name]
    lookup = {name: name for name in names}
    if not names or not frappe.db.exists("DocType", "CRM Lead"):
        return lookup

    if not frappe.db.has_column("CRM Lead", "sr_duplicate_of_name"):
        return lookup

    primary_for_requested: dict[str, str] = {}
    for row in frappe.get_all(
        "CRM Lead",
        filters={"name": ["in", names]},
        fields=["name", "sr_duplicate_of_name"],
        limit_page_length=0,
    ):
        primary = row.get("sr_duplicate_of_name")
        if primary:
            lookup[primary] = row.name
            primary_for_requested[row.name] = primary
        else:
            primary_for_requested[row.name] = row.name

    primary_to_requested: dict[str, str] = {}
    for requested, primary in primary_for_requested.items():
        primary_to_requested.setdefault(primary, requested)

    primary_names = list(primary_to_requested)
    if primary_names:
        for row in frappe.get_all(
            "CRM Lead",
            filters={"sr_duplicate_of_name": ["in", primary_names]},
            fields=["name", "sr_duplicate_of_name"],
            limit_page_length=0,
        ):
            requested = primary_to_requested.get(row.get("sr_duplicate_of_name"))
            if requested:
                lookup[row.name] = requested

    return lookup


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


def _reference_phone_numbers(doc) -> list[str]:
    meta = frappe.get_meta(doc.doctype)
    numbers = []
    for fieldname in (
        "mobile",
        "mobile_no",
        "phone",
        "whatsapp_number",
        "whatsapp_no",
        "custom_whatsapp_number",
    ):
        if not meta.has_field(fieldname):
            continue
        normalized = normalize_phone(doc.get(fieldname))
        if normalized and normalized not in numbers:
            numbers.append(normalized)
    return numbers


def _find_existing_conversation_by_phone(phone_number: str) -> str | None:
    normalized = normalize_phone(phone_number)
    if not normalized:
        return None

    contact_names = frappe.get_all(
        "Chat Contact",
        filters={"phone_number": normalized},
        pluck="name",
        limit_page_length=20,
    )
    last10 = normalized[-10:] if len(normalized) >= 10 else normalized
    for row in frappe.get_all(
        "Chat Contact",
        filters={"phone_number": ["like", f"%{last10}%"]},
        fields=["name", "phone_number"],
        limit_page_length=50,
    ):
        value = normalize_phone(row.get("phone_number"))
        if value and (value == normalized or value.endswith(last10)):
            if row.name not in contact_names:
                contact_names.append(row.name)

    if not contact_names:
        return None

    return frappe.db.get_value(
        "Chat Conversation",
        {"contact": ["in", contact_names], "status": ["!=", "Closed"]},
        "name",
        order_by="modified desc",
    ) or frappe.db.get_value(
        "Chat Conversation",
        {"contact": ["in", contact_names]},
        "name",
        order_by="modified desc",
    )


@frappe.whitelist()
def get_existing_conversation_for_patient(patient):
    if not patient:
        frappe.throw(_("patient is required"))
    if not frappe.db.exists("Patient", patient):
        frappe.throw(_("Patient {0} not found").format(patient))

    linked = _conversation_for_reference("Patient", patient)
    if linked:
        return {"success": True, "conversation": linked, "matched_by": "reference"}

    doc = frappe.get_doc("Patient", patient)
    phone_numbers = _reference_phone_numbers(doc)
    for phone_number in phone_numbers:
        conversation = _find_existing_conversation_by_phone(phone_number)
        if conversation:
            return {
                "success": True,
                "conversation": conversation,
                "matched_by": "phone",
                "phone_number": phone_number,
            }

    message = _("No existing WhatsApp chat found for this Patient number.")
    if not phone_numbers:
        message = _("Add a mobile number on this Patient to open WhatsApp chat.")
    return {"success": False, "conversation": None, "message": message}


@frappe.whitelist()
def get_reference_chat_statuses(reference_doctype, reference_names=None):
    if not reference_doctype:
        frappe.throw(_("reference_doctype is required"))

    names = _parse_reference_names(reference_names)[:MAX_REFERENCE_STATUS_NAMES]
    result = {
        name: {"unread_count": 0, "conversation_count": 0, "last_message_time": ""}
        for name in names
    }
    if not names:
        return {"success": True, "result": result}

    conv_stats: dict[str, dict] = {
        name: {"unread_count": 0, "conversation_count": 0, "last_message_time": ""}
        for name in names
    }
    meta = frappe.get_meta("Chat Conversation")
    reference_lookup = {name: name for name in names}
    query_names = names

    if reference_doctype == "CRM Lead":
        reference_lookup = _crm_lead_reference_lookup(names)
        query_names = list(reference_lookup) or names

    if reference_doctype == "CRM Lead" and meta.has_field("linked_crm_lead"):
        for row in frappe.get_all(
            "Chat Conversation",
            filters={"linked_crm_lead": ["in", query_names]},
            fields=["name", "linked_crm_lead", "unread_count", "modified"],
        ):
            _accumulate_reference_chat_status(
                conv_stats,
                reference_lookup.get(row.linked_crm_lead, row.linked_crm_lead),
                row,
            )

    for row in frappe.get_all(
        "Chat Conversation",
        filters={
            "linked_reference_doctype": reference_doctype,
            "linked_reference_name": ["in", query_names],
        },
        fields=["name", "linked_reference_name", "unread_count", "modified"],
    ):
        _accumulate_reference_chat_status(
            conv_stats,
            reference_lookup.get(row.linked_reference_name, row.linked_reference_name),
            row,
        )

    if reference_doctype == "Patient Encounter":
        encounter_patient = {}
        for enc in frappe.get_all(
            "Patient Encounter",
            filters={"name": ["in", names]},
            fields=["name", "patient"],
        ):
            if enc.patient:
                encounter_patient[enc.patient] = enc.name

        if encounter_patient:
            for row in frappe.get_all(
                "Chat Conversation",
                filters={
                    "linked_reference_doctype": "Patient",
                    "linked_reference_name": ["in", list(encounter_patient)],
                },
                fields=["name", "linked_reference_name", "unread_count", "modified"],
            ):
                encounter = encounter_patient.get(row.linked_reference_name)
                if encounter:
                    _accumulate_reference_chat_status(conv_stats, encounter, row)

    for name in names:
        stats = conv_stats.get(name)
        if stats and stats["conversation_count"]:
            result[name] = stats

    return {"success": True, "result": result}


def _accumulate_reference_chat_status(stats_by_name: dict, reference_name: str | None, row) -> None:
    if not reference_name or reference_name not in stats_by_name:
        return
    stats = stats_by_name[reference_name]
    stats["unread_count"] += cint(row.get("unread_count"))
    stats["conversation_count"] += 1
    modified = row.get("modified") or ""
    if str(modified) > str(stats["last_message_time"] or ""):
        stats["last_message_time"] = modified


@frappe.whitelist()
def resolve_chat_for_reference(reference_doctype, reference_name=None, phone_number=None):
    if not reference_doctype:
        frappe.throw(_("reference_doctype is required"))

    if reference_name:
        conv = _conversation_for_reference(reference_doctype, reference_name)
        if conv:
            return {"success": True, "result": {"conversation": conv}}
        created = _try_create_conversation_for_reference(reference_doctype, reference_name)
        if created:
            return {"success": True, "result": {"conversation": created, "created": True}}
        if reference_doctype == "CRM Lead":
            return {"success": True, "result": {"conversation": None}}

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
