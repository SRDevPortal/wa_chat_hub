import frappe
import hashlib
import hmac
import json
from typing import Optional

from frappe import _

from wa_chat_hub.connector.registry import get_adapter
from wa_chat_hub.services import append_message, normalize_phone


INTERAKT_STATUS_TYPES = {
    "message_api_sent",
    "message_api_delivered",
    "message_api_read",
    "message_api_failed",
    "message_campaign_sent",
    "message_campaign_delivered",
    "message_campaign_read",
    "message_campaign_failed",
    "message_sent",
    "message_delivered",
    "message_read",
    "message_failed",
}


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
        msg.delivery_status = "Received"
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


@frappe.whitelist(allow_guest=True)
def receive_interakt():
    """
    Interakt webhook endpoint.
    Configure this URL in Interakt Developer Settings:
    /api/method/wa_chat_hub.api.webhook.receive_interakt
    """
    try:
        if frappe.request.method == "GET":
            channel_account = frappe.form_dict.get("channel_account") or frappe.form_dict.get("account")
            return {
                "success": True,
                "message": "Interakt webhook endpoint is reachable. Interakt delivers events via POST.",
                "channel_account": channel_account,
            }

        if frappe.request.method != "POST":
            frappe.local.response["http_status_code"] = 405
            return {"success": False, "message": "Only POST requests accepted"}

        raw_body = frappe.request.data or b"{}"
        payload = json.loads(raw_body)
        channel_account = _resolve_interakt_channel_account(payload, raw_body)

        payload["channel_account"] = channel_account
        adapter = get_adapter("Interakt")
        webhook_type = payload.get("type")

        if webhook_type == "message_received":
            event = adapter.normalize_inbound(payload)
            if event.channel_message_id and frappe.db.exists("Chat Message", {"channel_message_id": event.channel_message_id}):
                return {"success": True, "message": "Duplicate message ignored"}
            result = append_message(event.__dict__)
            frappe.db.commit()
            return {"success": True, "result": result}

        if webhook_type in INTERAKT_STATUS_TYPES:
            event = adapter.normalize_status(payload)
            result = _update_message_status(event.channel_message_id, event.delivery_status, payload)
            if not result.get("updated"):
                result = _create_interakt_outbound_from_webhook(payload, event.delivery_status)
            frappe.db.commit()
            return {"success": True, "result": result}

        result = _sync_unknown_interakt_message_webhook(payload, webhook_type)
        if result:
            frappe.db.commit()
            return {"success": True, "result": result}

        frappe.log_error(
            f"Type: {webhook_type}\nPayload: {frappe.as_json(payload)}",
            "Ignored Interakt Webhook Type",
        )
        return {"success": True, "message": f"Ignored Interakt webhook type: {webhook_type}"}
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Interakt Webhook Error")
        frappe.local.response["http_status_code"] = 400
        return {"success": False, "message": "Interakt webhook failed"}


def _resolve_interakt_channel_account(payload, raw_body: bytes):
    """Resolve Interakt channel account for single or multi-account setups."""
    requested = (
        frappe.form_dict.get("channel_account")
        or frappe.form_dict.get("account")
        or payload.get("channel_account")
    )
    accounts = frappe.get_all(
        "Chat Channel Account",
        filters={"channel_type": "Interakt", "is_active": 1},
        pluck="name",
    )
    if not accounts:
        frappe.throw(_("No active Interakt Chat Channel Account found"))

    if requested:
        if requested not in accounts:
            frappe.throw(_("Unknown Chat Channel Account: {0}").format(requested))
        account = frappe.get_doc("Chat Channel Account", requested)
        _verify_interakt_signature(account, raw_body)
        return requested

    if len(accounts) == 1:
        account = frappe.get_doc("Chat Channel Account", accounts[0])
        _verify_interakt_signature(account, raw_body)
        return accounts[0]

    from_payload = _match_interakt_channel_from_payload(payload, accounts)
    if from_payload:
        account = frappe.get_doc("Chat Channel Account", from_payload)
        _verify_interakt_signature(account, raw_body)
        return from_payload

    signature_matches = _match_interakt_channels_by_signature(accounts, raw_body)
    if len(signature_matches) == 1:
        return signature_matches[0]
    if len(signature_matches) > 1:
        frappe.throw(
            _(
                "Ambiguous Interakt webhook: multiple channel accounts matched signature. "
                "Use a unique webhook secret per account or pass channel_account in webhook URL."
            )
        )

    frappe.throw(
        _(
            "Could not resolve Interakt channel account. "
            "Set a unique Interakt Webhook Secret per Chat Channel Account, "
            "or configure webhook URL with ?channel_account=<Account Name>."
        )
    )


def _match_interakt_channel_from_payload(payload, account_names: list) -> Optional[str]:
    """Best-effort account match using phone/waba identifiers in webhook payload."""
    data = payload.get("data") or {}
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    customer = data.get("customer") if isinstance(data.get("customer"), dict) else {}

    candidate_values = []
    for source in (payload, data, message, customer):
        if not isinstance(source, dict):
            continue
        for key in (
            "phone_number",
            "phone_id",
            "waba_id",
            "channel_phone_number",
            "business_phone_number",
            "from",
            "to",
            "receiver",
            "sender",
        ):
            value = source.get(key)
            if value:
                candidate_values.append(str(value))

    if not candidate_values:
        return None

    normalized_candidates = {normalize_phone(value) for value in candidate_values if normalize_phone(value)}
    rows = frappe.get_all(
        "Chat Channel Account",
        filters={"name": ["in", account_names]},
        fields=["name", "phone_number", "phone_id", "waba_id"],
    )
    matches = []
    for row in rows:
        row_tokens = {
            normalize_phone(row.phone_number),
            normalize_phone(row.phone_id),
            str(row.waba_id or "").strip(),
            str(row.name or "").strip(),
        }
        row_tokens.discard("")
        if row_tokens.intersection(normalized_candidates):
            matches.append(row.name)
            continue
        phone = normalize_phone(row.phone_number)
        if phone and any(phone.endswith(c[-10:]) or c.endswith(phone[-10:]) for c in normalized_candidates if len(c) >= 10):
            matches.append(row.name)

    if len(matches) == 1:
        return matches[0]
    return None


def _get_interakt_signature_header() -> str:
    return (
        frappe.get_request_header("Interakt-Signature")
        or frappe.get_request_header("X-Interakt-Signature")
        or ""
    )


def _interakt_signature_matches(secret: str, raw_body: bytes, received: str) -> bool:
    if not secret or not received:
        return False
    body = raw_body if isinstance(raw_body, bytes) else raw_body.encode("utf-8")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


def _match_interakt_channels_by_signature(account_names: list, raw_body: bytes) -> list:
    received = _get_interakt_signature_header()
    if not received:
        return []

    matched = []
    for name in account_names:
        account = frappe.get_doc("Chat Channel Account", name)
        secret = account.get_password("interakt_webhook_secret")
        if secret and _interakt_signature_matches(secret, raw_body, received):
            matched.append(name)
    return matched


def _verify_interakt_signature(account, raw_body: bytes) -> None:
    secret = account.get_password("interakt_webhook_secret")
    if not secret:
        frappe.throw(_("Interakt Webhook Secret is not configured for {0}").format(account.name))

    received = _get_interakt_signature_header()
    if not _interakt_signature_matches(secret, raw_body, received):
        frappe.throw(_("Invalid Interakt webhook signature for {0}").format(account.name))


def _create_interakt_outbound_from_webhook(payload, delivery_status):
    data = payload.get("data") or {}
    customer = data.get("customer") or {}
    message = data.get("message") or {}
    channel_message_id = message.get("id") or payload.get("channel_message_id")

    if channel_message_id and frappe.db.exists("Chat Message", {"channel_message_id": channel_message_id}):
        return {
            "updated": True,
            "message": frappe.db.get_value("Chat Message", {"channel_message_id": channel_message_id}, "name"),
            "delivery_status": delivery_status,
        }

    phone_number = (
        customer.get("channel_phone_number")
        or message.get("receiver")
        or message.get("to")
        or payload.get("phone_number")
    )
    if not phone_number:
        return {"updated": False, "message": "Missing customer phone for outbound sync"}

    body = _extract_interakt_message_body(message)
    content_type = message.get("message_content_type") or message.get("content_type") or message.get("type") or "Text"

    result = append_message({
        "channel_account": payload["channel_account"],
        "phone_number": phone_number,
        "display_name": _extract_interakt_customer_name(customer),
        "direction": "Outbound",
        "sender_type": "Agent",
        "content_type": str(content_type or "Text").title(),
        "body": body,
        "channel_message_id": channel_message_id,
        "delivery_status": delivery_status or "Sent",
        "raw_payload": payload,
    })
    return {"updated": False, "created": True, **result, "delivery_status": delivery_status}


def _sync_unknown_interakt_message_webhook(payload, webhook_type):
    data = payload.get("data") or {}
    message = data.get("message") or {}
    if not isinstance(message, dict) or not message:
        return None

    event_name = str(webhook_type or "").lower()
    message_status = str(message.get("message_status") or "").lower()
    message_category = str(message.get("category") or message.get("direction") or "").lower()
    if (
        "sent" not in event_name
        and "delivered" not in event_name
        and "read" not in event_name
        and "failed" not in event_name
        and message_category not in {"out", "outbound"}
    ):
        return None

    delivery_status = (
        "Read" if "read" in event_name or message_status == "read"
        else "Delivered" if "delivered" in event_name or message_status == "delivered"
        else "Failed" if "failed" in event_name or message_status == "failed"
        else "Sent"
    )
    return _create_interakt_outbound_from_webhook(payload, delivery_status)


def _extract_interakt_customer_name(customer):
    traits = customer.get("traits") or {}
    return traits.get("name") or customer.get("name")


def _extract_interakt_message_body(message):
    body = message.get("message") or message.get("text") or message.get("body")
    if isinstance(body, dict):
        body = body.get("body") or body.get("text") or body.get("message")
    if isinstance(body, str):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                body = parsed.get("body") or parsed.get("text") or parsed.get("message") or body
        except Exception:
            pass
    if str(body or "").strip().lower() in {"none", "null", "undefined"}:
        body = None
    if body:
        return body

    components = message.get("components") or []
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("type") or "").upper() == "BODY":
            return component.get("text") or "Template message"

    template = message.get("template") or {}
    if isinstance(template, dict):
        return template.get("name") or template.get("label") or "Template message"

    return f"{message.get('message_content_type') or message.get('type') or 'Message'} sent from Interakt"


def _update_message_status(channel_message_id, delivery_status, payload):
    if not channel_message_id:
        return {"updated": False, "message": "Missing Interakt message id"}

    message_name = (
        frappe.db.get_value("Chat Message", {"channel_message_id": channel_message_id}, "name")
        or frappe.db.get_value("Chat Message", {"provider_message_id": channel_message_id}, "name")
    )
    if not message_name:
        return {"updated": False, "message": f"No Chat Message found for {channel_message_id}"}

    updates = {"delivery_status": delivery_status or "Pending"}
    if frappe.get_meta("Chat Message").has_field("raw_payload"):
        updates["raw_payload"] = frappe.as_json(payload)
    frappe.db.set_value("Chat Message", message_name, updates)
    conversation = frappe.db.get_value("Chat Message", message_name, "conversation")
    frappe.publish_realtime(
        "wa_chat_message_status_updated",
        {
            "conversation": conversation,
            "message": message_name,
            "delivery_status": delivery_status,
        },
        after_commit=True,
    )
    return {"updated": True, "message": message_name, "delivery_status": delivery_status}
