import frappe

@frappe.whitelist()
def get_conversations():
    return frappe.db.sql("""
        select 
            c.name, c.channel_account, c.contact, c.status, c.unread_count,
            cnt.display_name, cnt.phone_number
        from `tabChat Conversation` c
        left join `tabChat Contact` cnt on c.contact = cnt.name
        order by c.modified desc
        limit 50
    """, as_dict=True)

@frappe.whitelist()
def get_messages(conversation):
    return frappe.db.sql("""
        select name, conversation, direction, content_type, body, media_url, creation
        from `tabChat Message`
        where conversation = %s
        order by creation asc
    """, (conversation,), as_dict=True)

import requests
import json

@frappe.whitelist()
def send_message(conversation, body):
    doc = frappe.new_doc("Chat Message")
    doc.conversation = conversation
    doc.direction = "Outbound"
    doc.content_type = "Text"
    doc.body = body
    
    conv_doc = frappe.get_doc("Chat Conversation", conversation)
    contact_doc = frappe.get_doc("Chat Contact", conv_doc.contact)
    
    channel = None
    if conv_doc.channel_account:
        channel = frappe.get_doc("Chat Channel Account", conv_doc.channel_account)
    else:
        active_channels = frappe.get_all("Chat Channel Account", filters={"is_active": 1})
        if active_channels:
            channel = frappe.get_doc("Chat Channel Account", active_channels[0].name)
            
    if channel and channel.provider_base_url:
        try:
            url = f"{channel.provider_base_url}/send-message?phoneId={channel.phone_id}&wabaId={channel.waba_id}"
            headers = {
                "x_api_key": channel.get_password("x_api_key") or channel.x_api_key,
                "x_user_id": channel.x_user_id,
                "x_tenant_id": channel.x_tenant_id,
                "Content-Type": "application/json"
            }
            # The integration specs
            payload = {
                "type": "text",
                "to": contact_doc.phone_number,
                "data": { "body": body }
            }
            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    doc.provider_message_id = data.get("result", {}).get("id")
            else:
                frappe.log_error(f"WA API Error: {resp.text}", "WA Sending Failed")
        except Exception as e:
            frappe.log_error(f"WA Connection Error: {str(e)}", "WA Sending Failed")
            
    doc.insert(ignore_permissions=True)
    frappe.publish_realtime("wa_chat_new_message", {"conversation": conversation, "message": doc.as_dict()})
    
    return doc.as_dict()
