import frappe
from frappe.utils import add_to_date, cint, now_datetime

from wa_chat_hub.permissions import conversation_access_sql_condition

NOTIFICATION_CACHE_TTL = 30
NOTIFICATION_SERVICE_ENABLED = False


def _disabled_counts():
    return {"all": 0, "crm_leads": 0, "patients": 0, "ai_replies": 0}


def _cache_key(prefix, **kwargs):
    parts = [f"user={frappe.session.user}"]
    parts.extend(f"{key}={value}" for key, value in sorted(kwargs.items()))
    return f"wa_chat_hub:notifications:{prefix}:" + "|".join(parts)


def _cache_get(key):
    try:
        return frappe.cache().get_value(key)
    except Exception:
        return None


def _cache_set(key, value, ttl=NOTIFICATION_CACHE_TTL):
    try:
        frappe.cache().set_value(key, value, expires_in_sec=ttl)
    except Exception:
        pass


def _doctype_ready(doctype, fields):
    try:
        if not frappe.db.table_exists(doctype):
            return False
        return all(frappe.db.has_column(doctype, fieldname) for fieldname in fields)
    except Exception:
        return False


def _notifications_ready():
    return (
        _doctype_ready("Chat Conversation", ["status", "unread_count", "contact"])
        and _doctype_ready("Chat Message", ["conversation", "direction", "body"])
        and _doctype_ready("Chat Contact", ["display_name", "phone_number"])
    )


def _has_column(doctype, fieldname):
    try:
        return frappe.db.has_column(doctype, fieldname)
    except Exception:
        return False


def _category_condition(category, conv_alias="c", msg_alias="m"):
    category = (category or "all").strip().lower()
    conditions = []
    values = []

    if category == "crm_leads":
        lead_conditions = []
        if _has_column("Chat Conversation", "linked_crm_lead"):
            lead_conditions.append(f"IFNULL({conv_alias}.linked_crm_lead, '') != ''")
        if _has_column("Chat Conversation", "linked_reference_doctype"):
            lead_conditions.append(f"{conv_alias}.linked_reference_doctype in ('CRM Lead', 'Lead')")
        conditions.append("(" + " or ".join(lead_conditions or ["1 = 0"]) + ")")
    elif category == "patients":
        if _has_column("Chat Conversation", "linked_reference_doctype"):
            conditions.append(f"{conv_alias}.linked_reference_doctype in ('Patient', 'Patient Encounter')")
        else:
            conditions.append("1 = 0")
    elif category == "ai_replies":
        conditions.append(f"{msg_alias}.direction = 'Outbound'")
        conditions.append(f"{msg_alias}.sender_type = 'AI'")
    else:
        conditions.append(
            f"({msg_alias}.direction = 'Inbound' or "
            f"({msg_alias}.direction = 'Outbound' and {msg_alias}.sender_type = 'AI'))"
        )

    return " and ".join(conditions), values


def _unread_count_for_category(category=None):
    if not _doctype_ready("Chat Conversation", ["status", "unread_count"]):
        return 0

    conditions = ["c.status != 'Closed'", conversation_access_sql_condition("c")]
    values = []
    category = (category or "all").strip().lower()

    if category == "crm_leads":
        lead_conditions = []
        if _has_column("Chat Conversation", "linked_crm_lead"):
            lead_conditions.append("IFNULL(c.linked_crm_lead, '') != ''")
        if _has_column("Chat Conversation", "linked_reference_doctype"):
            lead_conditions.append("c.linked_reference_doctype in ('CRM Lead', 'Lead')")
        conditions.append("(" + " or ".join(lead_conditions or ["1 = 0"]) + ")")
    elif category == "patients":
        if _has_column("Chat Conversation", "linked_reference_doctype"):
            conditions.append("c.linked_reference_doctype in ('Patient', 'Patient Encounter')")
        else:
            conditions.append("1 = 0")

    count = frappe.db.sql(
        f"""
        select coalesce(sum(c.unread_count), 0)
        from `tabChat Conversation` c
        where {" and ".join(conditions)}
        """,
        values,
    )[0][0]
    return int(count or 0)


def _unread_counts_by_category():
    if not _doctype_ready("Chat Conversation", ["status", "unread_count"]):
        return {"all": 0, "crm_leads": 0, "patients": 0}

    conditions = ["c.status != 'Closed'", conversation_access_sql_condition("c")]
    crm_condition = "1 = 0"
    patient_condition = "1 = 0"

    if _has_column("Chat Conversation", "linked_crm_lead") or _has_column(
        "Chat Conversation", "linked_reference_doctype"
    ):
        lead_conditions = []
        if _has_column("Chat Conversation", "linked_crm_lead"):
            lead_conditions.append("IFNULL(c.linked_crm_lead, '') != ''")
        if _has_column("Chat Conversation", "linked_reference_doctype"):
            lead_conditions.append("c.linked_reference_doctype in ('CRM Lead', 'Lead')")
        crm_condition = "(" + " or ".join(lead_conditions or ["1 = 0"]) + ")"

    if _has_column("Chat Conversation", "linked_reference_doctype"):
        patient_condition = "c.linked_reference_doctype in ('Patient', 'Patient Encounter')"

    row = frappe.db.sql(
        f"""
        select
            coalesce(sum(c.unread_count), 0) as all_count,
            coalesce(sum(case when {crm_condition} then c.unread_count else 0 end), 0) as crm_leads_count,
            coalesce(sum(case when {patient_condition} then c.unread_count else 0 end), 0) as patients_count
        from `tabChat Conversation` c
        where {" and ".join(conditions)}
        """,
        as_dict=True,
    )[0]
    return {
        "all": int(row.all_count or 0),
        "crm_leads": int(row.crm_leads_count or 0),
        "patients": int(row.patients_count or 0),
    }


def _recent_ai_reply_count():
    if not _doctype_ready("Chat Message", ["direction", "sender_type", "creation"]):
        return 0

    since = add_to_date(now_datetime(), hours=-24)
    count = frappe.db.sql(
        f"""
        select count(*)
        from `tabChat Message` m
        join `tabChat Conversation` c on m.conversation = c.name
        where m.direction = 'Outbound'
          and m.sender_type = 'AI'
          and m.creation >= %s
          and {conversation_access_sql_condition("c")}
        """,
        [since],
    )[0][0]
    return int(count or 0)


@frappe.whitelist()
def get_unread_count():
    if not NOTIFICATION_SERVICE_ENABLED:
        return 0

    try:
        key = _cache_key("unread_count")
        cached = _cache_get(key)
        if cached is not None:
            return cached
        value = _unread_counts_by_category()["all"] + _recent_ai_reply_count()
        _cache_set(key, value)
        return value
    except Exception as e:
        frappe.log_error(str(e), "Navbar Unread Count Error")
        return 0


@frappe.whitelist()
def get_notification_counts():
    if not NOTIFICATION_SERVICE_ENABLED:
        return _disabled_counts()

    try:
        key = _cache_key("counts")
        cached = _cache_get(key)
        if cached is not None:
            return cached
        unread_counts = _unread_counts_by_category()
        ai_replies = _recent_ai_reply_count()
        value = {
            "all": unread_counts["all"] + ai_replies,
            "crm_leads": unread_counts["crm_leads"],
            "patients": unread_counts["patients"],
            "ai_replies": ai_replies,
        }
        _cache_set(key, value)
        return value
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Navbar Notification Count Error")
        return {"all": 0, "crm_leads": 0, "patients": 0, "ai_replies": 0}


@frappe.whitelist()
def get_recent_messages():
    if not NOTIFICATION_SERVICE_ENABLED:
        return []

    try:
        if not _notifications_ready():
            return []

        # Fetch 10 most recent inbound messages with their contact names
        res = frappe.db.sql(f"""
            select 
                m.name, m.body, m.creation,
                IFNULL(cnt.display_name, cnt.phone_number) as sender_name
            from `tabChat Message` m
            join `tabChat Conversation` c on m.conversation = c.name
            join `tabChat Contact` cnt on c.contact = cnt.name
            where m.direction = 'Inbound'
              and {conversation_access_sql_condition("c")}
            order by m.creation desc
            limit 10
        """, as_dict=True)
        return res
    except Exception as e:
        frappe.log_error(str(e), "Navbar Notification Error")
        return []


@frappe.whitelist()
def get_recent_notifications(category="all", limit=10):
    if not NOTIFICATION_SERVICE_ENABLED:
        return []

    try:
        if not _notifications_ready():
            return []

        limit = max(1, min(cint(limit) or 10, 50))
        key = _cache_key("recent", category=category or "all", limit=limit)
        cached = _cache_get(key)
        if cached is not None:
            return cached

        condition, values = _category_condition(category)
        fields = [
            "m.name",
            "m.body",
            "m.creation",
            "m.direction",
            "m.sender_type",
            "m.conversation",
            "IFNULL(cnt.display_name, cnt.phone_number) as sender_name",
            "cnt.phone_number",
        ]
        if _has_column("Chat Conversation", "linked_reference_doctype"):
            fields.extend(["c.linked_reference_doctype", "c.linked_reference_name"])
        else:
            fields.extend(["'' as linked_reference_doctype", "'' as linked_reference_name"])
        if _has_column("Chat Conversation", "linked_crm_lead"):
            fields.append("c.linked_crm_lead")
        else:
            fields.append("'' as linked_crm_lead")

        rows = frappe.db.sql(
            f"""
            select {", ".join(fields)}
            from `tabChat Message` m
            join `tabChat Conversation` c on m.conversation = c.name
            join `tabChat Contact` cnt on c.contact = cnt.name
            where {condition}
              and {conversation_access_sql_condition("c")}
            order by m.creation desc
            limit %s
            """,
            values + [limit],
            as_dict=True,
        )
        _cache_set(key, rows)
        return rows
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Navbar Recent Notifications Error")
        return []
