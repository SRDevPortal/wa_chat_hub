import frappe


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


@frappe.whitelist()
def get_unread_count():
    try:
        if not _doctype_ready("Chat Conversation", ["status", "unread_count"]):
            return 0

        count = frappe.db.sql("""
            select coalesce(sum(unread_count), 0)
            from `tabChat Conversation`
            where status != 'Closed'
        """)[0][0]
        return int(count or 0)
    except Exception as e:
        frappe.log_error(str(e), "Navbar Unread Count Error")
        return 0


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
