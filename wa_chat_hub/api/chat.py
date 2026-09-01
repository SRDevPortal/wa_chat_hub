from __future__ import annotations

import json
import mimetypes
import time
from urllib.parse import quote, urlparse

import frappe
import requests
from frappe import _
from frappe.desk.form import assign_to
from frappe.utils import cint, now_datetime

from wa_chat_hub.audit import record_conversation_change
from wa_chat_hub.messaging.attribution import get_conversation_attribution
from wa_chat_hub.messaging.windows import get_messaging_window_state
from wa_chat_hub.permissions import can_read_crm_lead
from wa_chat_hub.permissions import conversation_access_sql_condition
from wa_chat_hub.permissions import ensure_can_read_conversation
from wa_chat_hub.permissions import filter_accessible_conversation_rows
from wa_chat_hub.permissions import filter_accessible_reference_names
from wa_chat_hub.services import (
    _available_phone_index_filters,
    append_message,
    build_erp_actions,
    conversation_update_lock,
    mark_conversation_read,
)
from wa_chat_hub.services import normalize_phone
from wa_chat_hub.task_logger import elapsed, task_log

CHAT_HUB_SCOPE_DOCTYPES = {"CRM Lead", "Lead", "Patient", "Patient Encounter"}
MEDIA_PROXY_MAX_BYTES = 20 * 1024 * 1024
MEDIA_PROXY_CONTENT_TYPES = {"Image", "Video", "Audio", "Document", "Sticker"}


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
CONVERSATION_CACHE_TTL = 5
CONVERSATION_SEARCH_CACHE_TTL = 10
CONVERSATION_SEARCH_MIN_TEXT_LENGTH = 2
CONVERSATION_SEARCH_MIN_PHONE_LENGTH = 4
CONVERSATION_SEARCH_MAX_QUERY_LENGTH = 140
CONVERSATION_SEARCH_SOURCE_LIMIT = 100
CONVERSATION_SEARCH_CANDIDATE_LIMIT = 2000
REFERENCE_STATUS_CACHE_TTL = 5
REFERENCE_CHAT_STATUS_ENABLED = False


def _has_conversation_last_message_time() -> bool:
    try:
        return frappe.db.has_column("Chat Conversation", "last_message_time")
    except Exception:
        return False


def _short_cache_get(key: str):
    try:
        return frappe.cache().get_value(key)
    except Exception:
        return None


def _short_cache_set(key: str, value, ttl: int) -> None:
    try:
        frappe.cache().set_value(key, value, expires_in_sec=ttl)
    except Exception:
        pass


def _api_cache_key(prefix: str, payload: dict) -> str:
    data = {
        "user": frappe.session.user,
        **payload,
    }
    return "wa_chat_hub:" + prefix + ":" + json.dumps(data, sort_keys=True, default=str)


def _conversation_list_filters(
    *,
    status=None,
    assigned_to=None,
    department=None,
    channel_account=None,
    reference_doctype=None,
    lead_temperature=None,
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
    if lead_temperature and frappe.get_meta("Chat Conversation").has_field("lead_temperature"):
        temperature = str(lead_temperature).strip().title()
        if temperature in {"Hot", "Warm", "Cold"}:
            filters["lead_temperature"] = temperature
    return filters


def _as_int(value, default=50, minimum=1, maximum=500) -> int:
    try:
        value = int(value)
    except Exception:
        value = default
    return max(minimum, min(value, maximum))


def _conversation_fetch_limit(limit) -> int:
    limit = _as_int(limit)
    return min(max(limit * 5, limit), 1000)


def _conversation_sql_rows(filters: dict, limit) -> list:
    conditions = [conversation_access_sql_condition("c")]
    values = {"limit": _conversation_fetch_limit(limit)}

    for index, (fieldname, value) in enumerate((filters or {}).items()):
        param = f"filter_{index}"
        if isinstance(value, (list, tuple)) and len(value) == 2 and str(value[0]).lower() == "in":
            options = tuple(value[1] or [])
            if not options:
                conditions.append("1 = 0")
                continue
            conditions.append(f"c.`{fieldname}` in %({param})s")
            values[param] = options
        else:
            conditions.append(f"c.`{fieldname}` = %({param})s")
            values[param] = value

    has_last_message_time = _has_conversation_last_message_time()
    select_fields = list(CONVERSATION_LIST_FIELDS)
    if has_last_message_time:
        select_fields.append("last_message_time")
    fields = ", ".join(f"c.`{fieldname}`" for fieldname in select_fields)
    order_expression = "c.`last_message_time`" if has_last_message_time else "c.`modified`"
    return frappe.db.sql(
        f"""
        select {fields}
        from `tabChat Conversation` c
        where {" and ".join(f"({condition})" for condition in conditions)}
        order by {order_expression} desc
        limit %(limit)s
        """,
        values,
        as_dict=True,
    )


def _limit_visible_rows(rows: list, limit) -> list:
    return filter_accessible_conversation_rows(rows)[: _as_int(limit)]


def _enrich_conversation_rows(rows: list) -> list:
    contact_names = []
    for row in rows:
        data = row if isinstance(row, dict) else row.as_dict()
        if data.get("contact"):
            contact_names.append(data["contact"])

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
                "last_message_time": data.get("last_message_time") or data.get("modified"),
            }
        )
    return sorted(enriched, key=lambda row: str(row.get("last_message_time") or ""), reverse=True)


def _phone_search_value(query: str) -> str:
    compact = "".join(character for character in str(query or "") if character not in " +-()./")
    if not compact.isdigit():
        return ""
    return normalize_phone(query)


def _matching_contact_names(query: str) -> list[str]:
    phone = _phone_search_value(query)
    if phone:
        return frappe.get_all(
            "Chat Contact",
            filters={"phone_number": phone},
            pluck="name",
            limit_page_length=CONVERSATION_SEARCH_SOURCE_LIMIT,
        )

    q_like = f"%{query}%"
    or_filters = [["display_name", "like", q_like], ["name", "like", q_like]]
    if phone and len(phone) >= 4:
        or_filters.append(["phone_number", "like", f"%{phone[-10:]}%"])
    else:
        or_filters.append(["phone_number", "like", q_like])
    return frappe.get_all(
        "Chat Contact",
        or_filters=or_filters,
        pluck="name",
        limit_page_length=CONVERSATION_SEARCH_SOURCE_LIMIT,
    )


def _indexed_reference_phone_names(doctype: str, phone_fields: list[str], query: str) -> list[str]:
    meta = frappe.get_meta(doctype)
    names = []
    seen = set()
    for fieldname, value in _available_phone_index_filters(meta, phone_fields, query):
        for name in frappe.get_all(
            doctype,
            filters={fieldname: value},
            pluck="name",
            limit_page_length=CONVERSATION_SEARCH_SOURCE_LIMIT,
        ):
            if name not in seen:
                names.append(name)
                seen.add(name)
                if len(names) >= CONVERSATION_SEARCH_SOURCE_LIMIT:
                    return names
    return names


def _matching_reference_conversation_names(query: str, base_filters: dict) -> set[str]:
    names: set[str] = set()
    q_like = f"%{query}%"
    phone = _phone_search_value(query)

    if frappe.db.exists("DocType", "CRM Lead"):
        lead_meta = frappe.get_meta("CRM Lead")
        lead_phone_fields = [
            fieldname
            for fieldname in ("mobile_no", "phone", "mobile", "custom_whatsapp_number")
            if lead_meta.has_field(fieldname)
        ]
        lead_names = (
            _indexed_reference_phone_names("CRM Lead", lead_phone_fields, phone) if phone else []
        )
        if not lead_names and not phone:
            lead_or = [["lead_name", "like", q_like], ["name", "like", q_like]]
            phone_like = f"%{phone[-10:]}%" if phone else q_like
            for fieldname in lead_phone_fields:
                lead_or.append([fieldname, "like", phone_like])
            if lead_meta.has_field("email"):
                lead_or.append(["email", "like", q_like])
            lead_names = frappe.get_all(
                "CRM Lead",
                or_filters=lead_or,
                pluck="name",
                limit_page_length=CONVERSATION_SEARCH_SOURCE_LIMIT,
            )
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
        patient_phone_fields = [
            fieldname
            for fieldname in ("mobile", "mobile_no", "phone", "custom_whatsapp_number")
            if patient_meta.has_field(fieldname)
        ]
        patient_names = (
            _indexed_reference_phone_names("Patient", patient_phone_fields, phone) if phone else []
        )
        if not patient_names and not phone:
            patient_or = [["patient_name", "like", q_like], ["name", "like", q_like]]
            if patient_meta.has_field("sr_patient_id"):
                patient_or.append(["sr_patient_id", "like", q_like])
            phone_like = f"%{phone[-10:]}%" if phone else q_like
            for fieldname in patient_phone_fields:
                patient_or.append([fieldname, "like", phone_like])
            patient_names = frappe.get_all(
                "Patient",
                or_filters=patient_or,
                pluck="name",
                limit_page_length=CONVERSATION_SEARCH_SOURCE_LIMIT,
            )
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


def _bounded_conversation_search_names(query: str, base_filters: dict, limit) -> set[str]:
    """Search a recent, indexed conversation window instead of scanning every source table."""
    conditions = [conversation_access_sql_condition("c")]
    values = {
        "query": f"%{query}%",
        "candidate_limit": CONVERSATION_SEARCH_CANDIDATE_LIMIT,
        "result_limit": _conversation_fetch_limit(limit),
    }

    for index, (fieldname, value) in enumerate((base_filters or {}).items()):
        param = f"search_filter_{index}"
        if isinstance(value, (list, tuple)) and len(value) == 2 and str(value[0]).lower() == "in":
            options = tuple(value[1] or [])
            if not options:
                conditions.append("1 = 0")
                continue
            conditions.append(f"c.`{fieldname}` in %({param})s")
            values[param] = options
        else:
            conditions.append(f"c.`{fieldname}` = %({param})s")
            values[param] = value

    conversation_meta = frappe.get_meta("Chat Conversation")
    candidate_fields = [
        "name",
        "contact",
        "linked_reference_doctype",
        "linked_reference_name",
        "last_message_preview",
        "channel_account",
        "modified",
    ]
    if conversation_meta.has_field("linked_crm_lead"):
        candidate_fields.append("linked_crm_lead")
    if _has_conversation_last_message_time():
        candidate_fields.append("last_message_time")

    order_field = "last_message_time" if "last_message_time" in candidate_fields else "modified"
    search_conditions = [
        "recent.`name` like %(query)s",
        "recent.`linked_reference_name` like %(query)s",
        "recent.`last_message_preview` like %(query)s",
        "recent.`channel_account` like %(query)s",
        "contact.`name` like %(query)s",
        "contact.`display_name` like %(query)s",
        "contact.`phone_number` like %(query)s",
    ]
    joins = ["left join `tabChat Contact` contact on contact.`name` = recent.`contact`"]

    if "linked_crm_lead" in candidate_fields:
        search_conditions.append("recent.`linked_crm_lead` like %(query)s")

    if frappe.db.exists("DocType", "CRM Lead"):
        lead_meta = frappe.get_meta("CRM Lead")
        joins.append(
            "left join `tabCRM Lead` lead on "
            "lead.`name` = coalesce(nullif(recent.`linked_crm_lead`, ''), "
            "case when recent.`linked_reference_doctype` in ('CRM Lead', 'Lead') "
            "then recent.`linked_reference_name` end)"
            if "linked_crm_lead" in candidate_fields
            else "left join `tabCRM Lead` lead on "
            "recent.`linked_reference_doctype` in ('CRM Lead', 'Lead') "
            "and lead.`name` = recent.`linked_reference_name`"
        )
        for fieldname in ("name", "lead_name", "email", "mobile_no", "phone", "mobile", "custom_whatsapp_number"):
            if fieldname == "name" or lead_meta.has_field(fieldname):
                search_conditions.append(f"lead.`{fieldname}` like %(query)s")

    if frappe.db.exists("DocType", "Patient"):
        patient_meta = frappe.get_meta("Patient")
        joins.append(
            "left join `tabPatient` patient on recent.`linked_reference_doctype` = 'Patient' "
            "and patient.`name` = recent.`linked_reference_name`"
        )
        for fieldname in (
            "name",
            "patient_name",
            "sr_patient_id",
            "mobile",
            "mobile_no",
            "phone",
            "custom_whatsapp_number",
        ):
            if fieldname == "name" or patient_meta.has_field(fieldname):
                search_conditions.append(f"patient.`{fieldname}` like %(query)s")

    fields_sql = ", ".join(f"c.`{fieldname}`" for fieldname in candidate_fields)
    rows = frappe.db.sql(
        f"""
        select recent.`name`
        from (
            select {fields_sql}
            from `tabChat Conversation` c
            where {" and ".join(f"({condition})" for condition in conditions)}
            order by c.`{order_field}` desc
            limit %(candidate_limit)s
        ) recent
        {" ".join(joins)}
        where {" or ".join(f"({condition})" for condition in search_conditions)}
        order by recent.`{order_field}` desc
        limit %(result_limit)s
        """,
        values,
        pluck=True,
    )
    return set(rows)


@frappe.whitelist()
def get_conversations(
    limit=50,
    status=None,
    assigned_to=None,
    department=None,
    channel_account=None,
    reference_doctype=None,
    lead_temperature=None,
):
    reference_doctype = _force_scoped_reference_doctype(reference_doctype)
    cache_key = _api_cache_key(
        "get_conversations",
        {
            "limit": limit,
            "status": status,
            "assigned_to": assigned_to,
            "department": department,
            "channel_account": channel_account,
            "reference_doctype": reference_doctype,
            "lead_temperature": lead_temperature,
        },
    )
    cached = _short_cache_get(cache_key)
    if cached is not None:
        return {"success": True, "result": cached}

    filters = _conversation_list_filters(
        status=status,
        assigned_to=assigned_to,
        department=department,
        channel_account=channel_account,
        reference_doctype=reference_doctype,
        lead_temperature=lead_temperature,
    )

    rows = _conversation_sql_rows(filters, limit)

    result = _enrich_conversation_rows(rows[: _as_int(limit)])
    _short_cache_set(cache_key, result, CONVERSATION_CACHE_TTL)
    return {"success": True, "result": result}


@frappe.whitelist()
def search_conversations(
    query,
    limit=100,
    status=None,
    assigned_to=None,
    department=None,
    channel_account=None,
    reference_doctype=None,
    lead_temperature=None,
):
    """Search conversations by phone, name, lead, patient, or message preview."""
    reference_doctype = _force_scoped_reference_doctype(reference_doctype)
    q = (query or "").strip()[:CONVERSATION_SEARCH_MAX_QUERY_LENGTH]
    if not q:
        return get_conversations(
            limit=limit,
            status=status,
            assigned_to=assigned_to,
            department=department,
            channel_account=channel_account,
            reference_doctype=reference_doctype,
            lead_temperature=lead_temperature,
        )

    phone = _phone_search_value(q)
    if (phone and len(phone) < CONVERSATION_SEARCH_MIN_PHONE_LENGTH) or (
        not phone and len(q) < CONVERSATION_SEARCH_MIN_TEXT_LENGTH
    ):
        return {"success": True, "result": []}

    cache_key = _api_cache_key(
        "search_conversations",
        {
            "query": q.casefold(),
            "limit": _as_int(limit),
            "status": status,
            "assigned_to": assigned_to,
            "department": department,
            "channel_account": channel_account,
            "reference_doctype": reference_doctype,
            "lead_temperature": lead_temperature,
        },
    )
    cached = _short_cache_get(cache_key)
    if cached is not None:
        return {"success": True, "result": cached}

    base_filters = _conversation_list_filters(
        status=status,
        assigned_to=assigned_to,
        department=department,
        channel_account=channel_account,
        reference_doctype=reference_doctype,
        lead_temperature=lead_temperature,
    )
    matching: set[str] = set()

    # Indexed exact phone lookups can find older conversations without scanning.
    # Text and compatibility matching are restricted to a recent candidate window.
    if phone:
        contact_names = _matching_contact_names(q)
        if contact_names:
            matching.update(
                frappe.get_all(
                    "Chat Conversation",
                    filters={**base_filters, "contact": ["in", contact_names]},
                    pluck="name",
                    limit_page_length=_conversation_fetch_limit(limit),
                )
            )
        matching.update(_matching_reference_conversation_names(q, base_filters))

    if not matching:
        matching.update(_bounded_conversation_search_names(q, base_filters, limit))

    if not matching:
        _short_cache_set(cache_key, [], CONVERSATION_SEARCH_CACHE_TTL)
        return {"success": True, "result": []}

    rows = frappe.get_all(
        "Chat Conversation",
        filters={"name": ["in", list(matching)]},
        fields=CONVERSATION_LIST_FIELDS,
        order_by="modified desc",
        limit_page_length=_conversation_fetch_limit(limit),
    )
    result = _enrich_conversation_rows(_limit_visible_rows(rows, limit))
    _short_cache_set(cache_key, result, CONVERSATION_SEARCH_CACHE_TTL)
    return {"success": True, "result": result}


@frappe.whitelist()
def get_messages(conversation, limit=100):
    ensure_can_read_conversation(conversation)
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
        order_by="creation desc",
        limit_page_length=int(limit),
    )
    rows.reverse()
    _attach_message_file_urls(rows)
    return {"success": True, "result": rows}


def _attach_message_file_urls(rows: list) -> None:
    file_names = [row.get("attachment_file") for row in rows if row.get("attachment_file")]
    if not file_names:
        _attach_message_media_proxy_urls(rows)
        return

    files = {
        row.name: row
        for row in frappe.get_all(
            "File",
            filters={"name": ["in", file_names]},
            fields=["name", "file_name", "file_url"],
        )
    }
    for row in rows:
        file_doc = files.get(row.get("attachment_file"))
        if not file_doc:
            continue
        row["attachment_url"] = file_doc.file_url
        row["attachment_file_name"] = file_doc.file_name
    _attach_message_media_proxy_urls(rows)


def _attach_message_media_proxy_urls(rows: list) -> None:
    for row in rows:
        if row.get("media_url") and row.get("content_type") in MEDIA_PROXY_CONTENT_TYPES:
            row["media_proxy_url"] = (
                "/api/method/wa_chat_hub.api.chat.get_message_media"
                f"?message={quote(str(row.get('name') or ''))}"
            )


@frappe.whitelist()
def get_message_media(message):
    row = frappe.db.get_value(
        "Chat Message",
        message,
        ["name", "conversation", "content_type", "media_url", "attachment_file"],
        as_dict=True,
    )
    if not row:
        frappe.throw(_("Message not found"))
    ensure_can_read_conversation(row.conversation)

    media_url = str(row.media_url or "").strip()
    file_name = "wa-media"
    if row.attachment_file:
        file_doc = frappe.db.get_value(
            "File",
            row.attachment_file,
            ["file_name", "file_url"],
            as_dict=True,
        )
        if file_doc:
            file_name = file_doc.file_name or file_name
            if not media_url:
                media_url = str(file_doc.file_url or "").strip()

    if not media_url:
        frappe.throw(_("Media URL not found"))
    parsed = urlparse(media_url)
    if parsed.scheme not in {"http", "https"}:
        frappe.throw(_("Unsupported media URL"))

    response = requests.get(media_url, timeout=25, stream=True)
    response.raise_for_status()

    content_length = cint(response.headers.get("content-length"))
    if content_length and content_length > MEDIA_PROXY_MAX_BYTES:
        frappe.throw(_("Media file is too large to preview"))

    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        total += len(chunk)
        if total > MEDIA_PROXY_MAX_BYTES:
            frappe.throw(_("Media file is too large to preview"))
        chunks.append(chunk)

    content_type = response.headers.get("content-type") or mimetypes.guess_type(media_url.split("?", 1)[0])[0]
    frappe.response["type"] = "download"
    frappe.response["filename"] = file_name
    frappe.response["filecontent"] = b"".join(chunks)
    frappe.response["content_type"] = content_type or "application/octet-stream"
    frappe.response["display_content_as"] = "inline"


@frappe.whitelist(methods=["POST"])
def mark_read(conversation):
    started = time.monotonic()
    task_log("chat", "mark_read_start", conversation=conversation)
    ensure_can_read_conversation(conversation)
    mark_conversation_read(conversation)
    task_log("chat", "mark_read_done", conversation=conversation, duration_sec=elapsed(started))
    return {"success": True}


@frappe.whitelist(methods=["POST"])
def bulk_assign(conversations, user=None):
    names = _as_list(conversations)
    if not names:
        frappe.throw(_("Select at least one conversation"))

    updated = 0
    for name in names:
        _ensure_conversation_write(name)
        with conversation_update_lock(name):
            previous = frappe.db.get_value("Chat Conversation", name, "assigned_to")
            if (previous or None) == (user or None):
                continue
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
            record_conversation_change(
                name,
                "Assignment",
                fieldname="assigned_to",
                old_value=previous,
                new_value=user,
                source="wa_chat_hub.bulk_assign",
            )
            updated += 1

    frappe.db.commit()
    return {"success": True, "updated": updated}


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

    updated = 0
    for name in names:
        _ensure_conversation_write(name)
        with conversation_update_lock(name):
            previous = frappe.db.get_value("Chat Conversation", name, fieldname)
            if previous == value:
                continue
            frappe.db.set_value("Chat Conversation", name, fieldname, value)
            record_conversation_change(
                name,
                "Status" if fieldname == "status" else "Priority",
                fieldname=fieldname,
                old_value=previous,
                new_value=value,
                source="wa_chat_hub.bulk_update",
            )
            updated += 1

    frappe.db.commit()
    return {"success": True, "updated": updated}


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
    started = time.monotonic()
    task_log("chat", "sidebar_start", conversation=conversation)
    ensure_can_read_conversation(conversation)
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

    result = {
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
    task_log("chat", "sidebar_done", conversation=conversation, duration_sec=elapsed(started))
    return result


@frappe.whitelist()
def get_messaging_window(conversation):
    if not conversation:
        frappe.throw(_("conversation is required"))
    ensure_can_read_conversation(conversation)
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
def get_conversation_for_reference(reference_doctype, reference_name, channel_account=None):
    if not reference_doctype or not reference_name:
        frappe.throw(_("reference_doctype and reference_name are required"))
    if reference_doctype == "CRM Lead" and not can_read_crm_lead(reference_name):
        frappe.throw(_("Not permitted to access this CRM Lead chat"), frappe.PermissionError)

    conversation = _conversation_for_reference(reference_doctype, reference_name)
    if conversation:
        ensure_can_read_conversation(conversation)
        return {"success": True, "conversation": conversation}

    created = _try_create_conversation_for_reference(
        reference_doctype,
        reference_name,
        channel_account=channel_account,
    )
    if created:
        ensure_can_read_conversation(created)
        return {"success": True, "conversation": created, "created": True}

    message = _("No WhatsApp conversation found for this record.")
    if reference_doctype in ("CRM Lead", "Lead", "Customer"):
        doc = _load_reference_doc(reference_doctype, reference_name)
        if doc and not _reference_has_phone(doc):
            message = _("Add a mobile number on this record to open WhatsApp chat.")

    return {
        "success": False,
        "conversation": None,
        "message": message,
    }


def _load_reference_doc(reference_doctype: str, reference_name: str):
    if frappe.db.exists(reference_doctype, reference_name):
        return frappe.get_doc(reference_doctype, reference_name)
    return None


def _try_create_conversation_for_reference(
    reference_doctype: str,
    reference_name: str,
    *,
    channel_account: str | None = None,
) -> str | None:
    try:
        if reference_doctype in {"CRM Lead", "Lead"} and frappe.db.exists(reference_doctype, reference_name):
            lead = frappe.get_doc(reference_doctype, reference_name)
            if not _reference_has_phone(lead):
                return None
            if channel_account:
                from wa_chat_hub.channel_resolver import get_or_create_lead_conversation_for_channel_account

                return get_or_create_lead_conversation_for_channel_account(
                    lead,
                    channel_account,
                )["conversation"]

            from wa_chat_hub.channel_resolver import get_or_create_mapped_lead_conversation

            return get_or_create_mapped_lead_conversation(lead)["conversation"]

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
        ensure_can_read_conversation(linked)
        return {"success": True, "conversation": linked, "matched_by": "reference"}

    doc = frappe.get_doc("Patient", patient)
    phone_numbers = _reference_phone_numbers(doc)
    for phone_number in phone_numbers:
        conversation = _find_existing_conversation_by_phone(phone_number)
        if conversation:
            ensure_can_read_conversation(conversation)
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
    if not REFERENCE_CHAT_STATUS_ENABLED:
        return {"success": True, "result": {}}

    if not reference_doctype:
        frappe.throw(_("reference_doctype is required"))

    names = _parse_reference_names(reference_names)[:MAX_REFERENCE_STATUS_NAMES]
    result = {
        name: {"unread_count": 0, "conversation_count": 0, "last_message_time": ""}
        for name in names
    }
    if not names:
        return {"success": True, "result": result}

    if reference_doctype != "CRM Lead":
        return {"success": True, "result": result}

    cache_key = _api_cache_key(
        "reference_statuses",
        {
            "reference_doctype": reference_doctype,
            "names": names,
        },
    )
    cached = _short_cache_get(cache_key)
    if cached is not None:
        return {"success": True, "result": cached}

    allowed_names = filter_accessible_reference_names(reference_doctype, names)
    if not allowed_names:
        return {"success": True, "result": result}

    conv_stats: dict[str, dict] = {
        name: {"unread_count": 0, "conversation_count": 0, "last_message_time": ""}
        for name in allowed_names
    }
    meta = frappe.get_meta("Chat Conversation")
    reference_lookup = {name: name for name in allowed_names}
    query_names = allowed_names

    if reference_doctype == "CRM Lead":
        reference_lookup = _crm_lead_reference_lookup(allowed_names)
        query_names = list(reference_lookup) or allowed_names

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
            filters={"name": ["in", allowed_names]},
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

    _short_cache_set(cache_key, result, REFERENCE_STATUS_CACHE_TTL)
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
        if reference_doctype == "CRM Lead" and not can_read_crm_lead(reference_name):
            frappe.throw(_("Not permitted to access this CRM Lead chat"), frappe.PermissionError)
        conv = _conversation_for_reference(reference_doctype, reference_name)
        if conv:
            ensure_can_read_conversation(conv)
            return {"success": True, "result": {"conversation": conv}}
        created = _try_create_conversation_for_reference(reference_doctype, reference_name)
        if created:
            ensure_can_read_conversation(created)
            return {"success": True, "result": {"conversation": created, "created": True}}
        if reference_doctype == "CRM Lead":
            return {"success": True, "result": {"conversation": None}}

    normalized = normalize_phone(phone_number)
    if normalized:
        contact = frappe.db.get_value("Chat Contact", {"phone_number": normalized}, "name")
        if contact:
            conv = frappe.db.get_value("Chat Conversation", {"contact": contact, "status": ["!=", "Closed"]}, "name")
            if conv:
                ensure_can_read_conversation(conv)
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
    ensure_can_read_conversation(name)
    if not frappe.has_permission("Chat Conversation", "write", name):
        frappe.throw(_("Not permitted to update conversation {0}").format(name), frappe.PermissionError)
