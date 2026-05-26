import frappe
from frappe.utils import add_to_date, cint, now_datetime


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

    conditions = ["status != 'Closed'"]
    values = []
    category = (category or "all").strip().lower()

    if category == "crm_leads":
        lead_conditions = []
        if _has_column("Chat Conversation", "linked_crm_lead"):
            lead_conditions.append("IFNULL(linked_crm_lead, '') != ''")
        if _has_column("Chat Conversation", "linked_reference_doctype"):
            lead_conditions.append("linked_reference_doctype in ('CRM Lead', 'Lead')")
        conditions.append("(" + " or ".join(lead_conditions or ["1 = 0"]) + ")")
    elif category == "patients":
        if _has_column("Chat Conversation", "linked_reference_doctype"):
            conditions.append("linked_reference_doctype in ('Patient', 'Patient Encounter')")
        else:
            conditions.append("1 = 0")

    count = frappe.db.sql(
        f"""
        select coalesce(sum(unread_count), 0)
        from `tabChat Conversation`
        where {" and ".join(conditions)}
        """,
        values,
    )[0][0]
    return int(count or 0)


def _recent_ai_reply_count():
    if not _doctype_ready("Chat Message", ["direction", "sender_type", "creation"]):
        return 0

    since = add_to_date(now_datetime(), hours=-24)
    count = frappe.db.sql(
        """
        select count(*)
        from `tabChat Message`
        where direction = 'Outbound'
          and sender_type = 'AI'
          and creation >= %s
        """,
        [since],
    )[0][0]
    return int(count or 0)


@frappe.whitelist()
def get_unread_count():
    try:
        return _unread_count_for_category() + _recent_ai_reply_count()
    except Exception as e:
        frappe.log_error(str(e), "Navbar Unread Count Error")
        return 0


@frappe.whitelist()
def get_notification_counts():
    try:
        return {
            "all": _unread_count_for_category() + _recent_ai_reply_count(),
            "crm_leads": _unread_count_for_category("crm_leads"),
            "patients": _unread_count_for_category("patients"),
            "ai_replies": _recent_ai_reply_count(),
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Navbar Notification Count Error")
        return {"all": 0, "crm_leads": 0, "patients": 0, "ai_replies": 0}


@frappe.whitelist()
def get_recent_messages():
    try:
        if not _notifications_ready():
            return []

        # Fetch 10 most recent inbound messages with their contact names
        res = frappe.db.sql("""
            select 
                m.name, m.body, m.creation,
                IFNULL(cnt.display_name, cnt.phone_number) as sender_name
            from `tabChat Message` m
            join `tabChat Conversation` c on m.conversation = c.name
            join `tabChat Contact` cnt on c.contact = cnt.name
            where m.direction = 'Inbound'
            order by m.creation desc
            limit 10
        """, as_dict=True)
        return res
    except Exception as e:
        frappe.log_error(str(e), "Navbar Notification Error")
        return []


@frappe.whitelist()
def get_recent_notifications(category="all", limit=10):
    try:
        if not _notifications_ready():
            return []

        limit = max(1, min(cint(limit) or 10, 50))
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

        return frappe.db.sql(
            f"""
            select {", ".join(fields)}
            from `tabChat Message` m
            join `tabChat Conversation` c on m.conversation = c.name
            join `tabChat Contact` cnt on c.contact = cnt.name
            where {condition}
            order by m.creation desc
            limit %s
            """,
            values + [limit],
            as_dict=True,
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Navbar Recent Notifications Error")
        return []
