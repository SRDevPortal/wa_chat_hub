import frappe


@frappe.whitelist()
def get_unread_count():
    try:
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
