import frappe
import json

@frappe.whitelist(allow_guest=True)
def receive():
    """
    Webhook endpoint to receive incoming WhatsApp messages from the external provider.
    Expected Route: /api/method/wa_chat_hub.api.webhook.receive
    """
    try:
        if frappe.request.method != "POST":
            return {"success": False, "message": "Only POST requests accepted"}
            
        payload = json.loads(frappe.request.data)
        
        # Determine sender (user) and recipient (our WABA)
        # Based on typical webhook schemas, "from" is the user, "to" is our WABA or Phone ID.
        user_phone = payload.get("from")
        body = payload.get("text", {}).get("body", "")
        message_id = payload.get("messageId")
        msg_type = payload.get("type", "text")
        
        if not user_phone:
            return {"success": False, "message": "Missing 'from' parameter"}
            
        if msg_type != "text" and not body:
            # Fallback for media if body is empty
            body = f"[{msg_type} message received]"
            
        frappe.set_user("Administrator")
        
        # 1. Match or Create Contact
        contact = frappe.db.get_value("Chat Contact", {"phone_number": user_phone}, "name")
        if not contact:
            # Let's create it
            c_doc = frappe.new_doc("Chat Contact")
            c_doc.phone_number = user_phone
            c_doc.display_name = user_phone
            c_doc.insert(ignore_permissions=True)
            contact = c_doc.name
            
        # 2. Match or Create Conversation
        conv = frappe.db.get_value("Chat Conversation", {"contact": contact, "status": "Open"}, "name")
        if not conv:
            conv_doc = frappe.new_doc("Chat Conversation")
            conv_doc.contact = contact
            conv_doc.status = "Open"
            
            # Try to map channel account from payload's 'to' if it matches phone_number or phone_id
            to_id = payload.get("to")
            if to_id:
                channel = frappe.db.get_value("Chat Channel Account", {"phone_id": to_id}, "name")
                if not channel:
                    channel = frappe.db.get_value("Chat Channel Account", {"phone_number": to_id}, "name")
                if channel:
                    conv_doc.channel_account = channel
            
            conv_doc.insert(ignore_permissions=True)
            conv = conv_doc.name
            
        # 3. Insert Chat Message
        msg = frappe.new_doc("Chat Message")
        msg.conversation = conv
        msg.direction = "Inbound"
        msg.content_type = "Text"
        msg.body = body
        if message_id:
            msg.provider_message_id = message_id
            
        # The AI Auto-reply is attached to 'after_insert' in hooks.py, so it will fire automatically!
        msg.insert(ignore_permissions=True)
        frappe.db.commit()
        
        # 4. Broadcast Realtime
        frappe.publish_realtime("wa_chat_new_message", {"conversation": conv, "message": msg.as_dict()})
        
        return {"success": True, "message": "Message received and processed."}
        
    except Exception as e:
        frappe.log_error(f"Webhook Receive Error: {str(e)}", "WA Webhook")
        return {"success": False, "message": str(e)}

