import frappe
import hashlib
import hmac
import json
import time
from typing import Optional

from frappe import _

from wa_chat_hub.connector.interakt.adapter import extract_interakt_customer_phone, extract_interakt_media_url
from wa_chat_hub.connector.registry import get_adapter
from wa_chat_hub.interakt.account_config import account_routing_context, get_interakt_account, should_verify_webhook_signature
from wa_chat_hub.services import append_message, normalize_phone
from wa_chat_hub.task_logger import elapsed, task_log


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
    started = time.monotonic()
    task_log("webhook", "receive_start", provider="generic")
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

        channel_account = None
        to_id = payload.get("to")
        if to_id:
            channel_account = frappe.db.get_value("Chat Channel Account", {"phone_id": to_id}, "name")
            if not channel_account:
                channel_account = frappe.db.get_value(
                    "Chat Channel Account", {"phone_number": to_id}, "name"
                )
        if not channel_account:
            channel_account = frappe.db.get_value(
                "Chat Channel Account",
                {"is_active": 1, "channel_type": "Interakt"},
                "name",
                order_by="modified desc",
            )
        if not channel_account:
            return {"success": False, "message": "No active Chat Channel Account configured"}

        result = append_message(
            {
                "channel_account": channel_account,
                "phone_number": user_phone,
                "direction": "Inbound",
                "content_type": "Text",
                "body": body,
                "delivery_status": "Received",
                "channel_message_id": message_id,
                "raw_transport_payload": payload,
            }
        )
        frappe.db.commit()
        task_log(
            "webhook",
            "receive_done",
            provider="generic",
            channel_account=channel_account,
            conversation=result.get("conversation"),
            message=result.get("message"),
            duration_sec=elapsed(started),
        )
        return {"success": True, "result": result}
        
    except Exception as e:
        task_log(
            "webhook",
            "receive_failed",
            provider="generic",
            duration_sec=elapsed(started),
            error=str(e)[:140],
        )
        frappe.log_error(f"Webhook Receive Error: {str(e)}", "WA Webhook")
        return {"success": False, "message": str(e)}


@frappe.whitelist()
def get_interakt_webhook_url(channel_account: str):
    """Public webhook URL for Interakt Developer Settings (uses site host_name / ngrok)."""
    from frappe.utils import get_url
    from urllib.parse import quote

    if not channel_account:
        frappe.throw(_("channel_account is required"))
    get_interakt_account(channel_account)
    path = (
        f"/api/method/wa_chat_hub.api.webhook.receive_interakt"
        f"?channel_account={quote(channel_account)}"
    )
    return {"success": True, "url": get_url(path)}


@frappe.whitelist(allow_guest=True)
def receive_interakt():
    """
    Interakt webhook endpoint.
    Configure this URL in Interakt Developer Settings:
    /api/method/wa_chat_hub.api.webhook.receive_interakt
    """
    started = time.monotonic()
    task_log("webhook", "receive_start", provider="Interakt")
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

        raw_body = frappe.request.get_data(cache=True) or b"{}"
        payload = json.loads(raw_body)
        channel_account = _resolve_interakt_channel_account(payload, raw_body)
        payload["channel_account"] = channel_account
        webhook_type = payload.get("type")

        queue = _interakt_webhook_queue(payload)
        job_id = _interakt_webhook_job_id(channel_account, payload, raw_body)
        run_now = bool(getattr(frappe.flags, "in_test", False))
        job = frappe.enqueue(
            "wa_chat_hub.api.webhook.process_interakt_webhook",
            queue=queue,
            payload=payload,
            raw_body=raw_body,
            channel_account=channel_account,
            timeout=900 if queue == "long" else 300,
            job_id=job_id,
            deduplicate=True,
            enqueue_after_commit=False,
            now=run_now,
        )
        task_log(
            "webhook",
            "receive_queued",
            provider="Interakt",
            channel_account=channel_account,
            webhook_type=webhook_type,
            queue=queue,
            job_id=job_id,
            duration_sec=elapsed(started),
        )
        return {
            "success": True,
            "queued": not run_now,
            "queue": queue,
            "job_id": job_id,
            "result": job if run_now else None,
        }
    except frappe.ValidationError as exc:
        task_log(
            "webhook",
            "receive_failed",
            provider="Interakt",
            duration_sec=elapsed(started),
            error=str(exc)[:140],
        )
        frappe.log_error(frappe.get_traceback(), "Interakt Webhook Validation")
        frappe.local.response["http_status_code"] = 400
        return {"success": False, "message": str(exc)}
    except Exception as exc:
        task_log(
            "webhook",
            "receive_failed",
            provider="Interakt",
            duration_sec=elapsed(started),
            error=str(exc)[:140],
        )
        frappe.log_error(frappe.get_traceback(), "Interakt Webhook Error")
        frappe.local.response["http_status_code"] = 400
        return {"success": False, "message": str(exc) or "Interakt webhook failed"}


def process_interakt_webhook(payload: dict, raw_body: str | bytes | None = None, channel_account: str | None = None):
    """Background worker for Interakt webhooks after the HTTP endpoint has acknowledged."""
    started = time.monotonic()
    frappe.set_user("Administrator")
    if not isinstance(payload, dict):
        payload = json.loads(payload or "{}")
    if channel_account:
        payload["channel_account"] = channel_account
    raw_body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else (raw_body or b"{}")
    try:
        result = _process_interakt_payload(payload, raw_body_bytes)
        frappe.db.commit()
        return result
    except Exception:
        frappe.db.rollback()
        task_log(
            "webhook",
            "worker_failed",
            provider="Interakt",
            channel_account=payload.get("channel_account"),
            webhook_type=payload.get("type"),
            duration_sec=elapsed(started),
            error=frappe.get_traceback()[-140:],
        )
        frappe.log_error(frappe.get_traceback(), "Interakt Webhook Worker Error")
        raise


def _process_interakt_payload(payload: dict, raw_body: bytes | None = None):
    channel_account = payload.get("channel_account") or _resolve_interakt_channel_account(payload, raw_body or b"{}")
    account_doc = get_interakt_account(channel_account)
    routing = account_routing_context(account_doc)

    payload["channel_account"] = channel_account
    adapter = get_adapter("Interakt")
    webhook_type = payload.get("type")
    started = time.monotonic()

    if webhook_type == "message_received":
        event = adapter.normalize_inbound(payload)
        normalized_phone = normalize_phone(event.phone_number)
        if not normalized_phone:
            customer = (payload.get("data") or {}).get("customer") or {}
            frappe.log_error(
                frappe.as_json(
                    {
                        "reason": "missing_customer_phone",
                        "channel_account": channel_account,
                        "webhook_type": webhook_type,
                        "customer_keys": list(customer.keys()) if isinstance(customer, dict) else [],
                        "extracted_phone": event.phone_number,
                        "payload_sample": payload,
                    }
                ),
                "Interakt Inbound Missing Phone",
            )
            return {
                "success": False,
                "message": "Could not resolve customer phone from Interakt payload",
            }

        if event.channel_message_id and frappe.db.exists("Chat Message", {"channel_message_id": event.channel_message_id}):
            return {"success": True, "message": "Duplicate message ignored"}

        event_dict = event.__dict__
        event_dict["phone_number"] = normalized_phone
        event_dict["channel_department"] = routing.get("channel_department")
        result = append_message(event_dict)
        task_log(
            "webhook",
            "inbound_done",
            provider="Interakt",
            channel_account=channel_account,
            conversation=result.get("conversation"),
            message=result.get("message"),
            duration_sec=elapsed(started),
        )
        return {"success": True, "result": result}

    if webhook_type in INTERAKT_STATUS_TYPES:
        event = adapter.normalize_status(payload)
        result = _update_message_status(event.channel_message_id, event.delivery_status, payload)
        if not result.get("updated"):
            result = _create_interakt_outbound_from_webhook(payload, event.delivery_status)
        task_log(
            "webhook",
            "status_done",
            provider="Interakt",
            channel_account=channel_account,
            delivery_status=event.delivery_status,
            updated=result.get("updated"),
            duration_sec=elapsed(started),
        )
        return {"success": True, "result": result}

    result = _sync_unknown_interakt_message_webhook(payload, webhook_type)
    if result:
        task_log(
            "webhook",
            "sync_done",
            provider="Interakt",
            channel_account=channel_account,
            webhook_type=webhook_type,
            duration_sec=elapsed(started),
        )
        return {"success": True, "result": result}

    frappe.log_error(
        f"Type: {webhook_type}\nPayload: {frappe.as_json(payload)}",
        "Ignored Interakt Webhook Type",
    )
    return {"success": True, "message": f"Ignored Interakt webhook type: {webhook_type}"}


def _interakt_webhook_queue(payload: dict) -> str:
    webhook_type = payload.get("type")
    message = ((payload.get("data") or {}).get("message") or {}) if isinstance(payload, dict) else {}
    if not isinstance(message, dict):
        message = {}
    content_type = str(
        message.get("message_content_type")
        or message.get("content_type")
        or message.get("type")
        or ""
    ).lower()
    if webhook_type == "message_received" and content_type in {"image", "video", "audio", "document", "sticker"}:
        return "long"
    return "short"


def _interakt_webhook_job_id(channel_account: str, payload: dict, raw_body: bytes) -> str:
    webhook_type = str(payload.get("type") or "unknown")
    message_id = _interakt_payload_message_id(payload)
    if message_id:
        token = f"{channel_account}:{webhook_type}:{message_id}"
    else:
        token = f"{channel_account}:{webhook_type}:{hashlib.sha256(raw_body or b'').hexdigest()}"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    return f"wa_interakt_webhook_{digest}"


def _interakt_payload_message_id(payload: dict) -> str:
    data = payload.get("data") or {}
    message = data.get("message") if isinstance(data.get("message"), dict) else {}
    return str(message.get("id") or payload.get("channel_message_id") or "").strip()


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
        account = frappe.get_doc("Chat Channel Account", signature_matches[0])
        _verify_interakt_signature(account, raw_body)
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
        or frappe.get_request_header("X-Hub-Signature-256")
        or frappe.get_request_header("X-Hub-Signature")
        or ""
    )


def _interakt_signature_variants(signature: str) -> set[str]:
    value = (signature or "").strip()
    if not value:
        return set()
    variants = {value}
    if value.lower().startswith("sha256="):
        variants.add(value.split("=", 1)[1].strip())
    else:
        variants.add(f"sha256={value}")
    return variants


def _interakt_signature_matches(secret: str, raw_body: bytes, received: str) -> bool:
    if not secret or not received:
        return False
    body = raw_body if isinstance(raw_body, bytes) else raw_body.encode("utf-8")
    secret = secret.strip()
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    expected = {digest, f"sha256={digest}"}
    received_variants = _interakt_signature_variants(received)
    return bool(expected & received_variants)


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
    if not should_verify_webhook_signature(account):
        return

    secret = account.get_password("interakt_webhook_secret")
    if not secret:
        frappe.throw(
            _(
                "Interakt Webhook Secret is not configured on Chat Channel Account {0}. "
                "Paste the secret from Interakt Developer Settings into this form."
            ).format(account.name)
        )

    received = _get_interakt_signature_header()
    if not received:
        frappe.throw(
            _(
                "Missing Interakt-Signature header for account {0}. "
                "Confirm the webhook secret in Interakt matches this Chat Channel Account."
            ).format(account.name)
        )

    if not _interakt_signature_matches(secret, raw_body, received):
        frappe.throw(
            _(
                "Invalid Interakt webhook signature for {0}. "
                "Re-copy Interakt Webhook Secret into Chat Channel Account and save."
            ).format(account.name)
        )


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
        extract_interakt_customer_phone(customer, payload)
        or message.get("receiver")
        or message.get("to")
        or payload.get("phone_number")
    )
    if not phone_number:
        return {"updated": False, "message": "Missing customer phone for outbound sync"}

    body = _extract_interakt_message_body(message)
    content_type = message.get("message_content_type") or message.get("content_type") or message.get("type") or "Text"
    media_url = extract_interakt_media_url(message) or extract_interakt_media_url(payload)

    result = append_message({
        "channel_account": payload["channel_account"],
        "phone_number": phone_number,
        "display_name": _extract_interakt_customer_name(customer),
        "direction": "Outbound",
        "sender_type": "Agent",
        "content_type": str(content_type or "Text").title(),
        "body": body,
        "media_url": media_url,
        "channel_message_id": channel_message_id,
        "delivery_status": delivery_status or "Sent",
        "raw_payload": payload,
        "raw_transport_payload": payload,
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
