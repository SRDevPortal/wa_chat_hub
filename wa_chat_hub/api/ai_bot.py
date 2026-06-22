import json
import re
import time
from types import SimpleNamespace

import frappe
import requests
from frappe.utils import cint
from frappe.utils.background_jobs import enqueue

from wa_chat_hub.ai.ocr_summary import GENERIC_MEDIA_BODIES, build_media_context_for_chat
from wa_chat_hub.ai.providers import CHAT_CAPABILITY, get_active_llm_provider_rows
from wa_chat_hub.ai.media_transcription import (
    TRANSCRIPT_CONTENT_TYPES,
    build_transcript_context_for_chat,
)
from wa_chat_hub.ai.delivery_status import build_delivery_status_reply, is_delivery_status_query
from wa_chat_hub.ai.clinical_history import (
    build_clinical_history_context,
    build_clinical_history_reply,
    is_clinical_history_query,
)
from wa_chat_hub.ai.conversation_stop import (
    FINAL_WARNING_MESSAGE,
    STOP_MESSAGE,
    evaluate_stop_rule,
    mark_conversation_stopped,
)
from wa_chat_hub.ai.service import create_ai_suggestion
from wa_chat_hub.api.vector_search import search_knowledge_base
from wa_chat_hub.outbound import send_outbound_message
from wa_chat_hub.prompts import (
    build_system_prompt_from_config,
    get_effective_prompt_config,
    get_multilingual_policy,
)
from wa_chat_hub.services import append_message
from wa_chat_hub.task_logger import elapsed, queue_wait_seconds, task_log

CONVERSATION_HISTORY_LIMIT = 40
MEDIA_CONTENT_TYPES = frozenset({"Image", "Video", "Audio", "Document", "Sticker"})


def _log_ai_timing(event: str, **fields) -> None:
    task_log("ai", event, **fields)


def _inside_append_message() -> bool:
    return bool(
        getattr(frappe.flags, "wa_chat_in_append_message", False)
        or getattr(frappe.local, "wa_chat_in_append_message", False)
    )


def on_message_received(doc, method):
    """Fallback when Chat Message is inserted outside append_message()."""
    if _inside_append_message():
        return
    if (doc.direction or "").strip() != "Inbound":
        return
    if (doc.sender_type or "").strip() in ("AI", "System", "Bot"):
        return
    schedule_autopilot_for_message(doc.name)


def schedule_autopilot_for_message(message_name: str) -> None:
    """Queue AI autopilot after inbound message, lead link, and messaging window are ready."""
    if not message_name or not frappe.db.exists("Chat Message", message_name):
        return

    doc = frappe.get_doc("Chat Message", message_name)
    if doc.direction != "Inbound":
        return
    if not _inbound_triggers_autopilot(doc):
        return

    settings = frappe.get_single("WA Chat Hub Settings")
    if not settings.enable_ai_autopilot:
        return
    if (settings.autopilot_mode or "Suggest Only") == "Disabled":
        return

    try:
        enqueue(
            "wa_chat_hub.api.ai_bot.process_message",
            queue="short",
            message_id=message_name,
            enqueue_after_commit=True,
            now=False,
            job_id=f"wa_ai_autopilot_{message_name}",
            deduplicate=True,
        )
        _log_ai_timing(
            "enqueue",
            message=message_name,
            conversation=getattr(doc, "conversation", None),
            content_type=getattr(doc, "content_type", None) or "Text",
            queue="short",
        )
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA AI Autopilot Enqueue Failed")


def process_message(message_id):
    total_started = time.monotonic()
    frappe.set_user("Administrator")

    msg_doc = frappe.get_doc("Chat Message", message_id)
    conversation = msg_doc.conversation
    _log_ai_timing(
        "start",
        message=message_id,
        conversation=conversation,
        queue_wait_sec=queue_wait_seconds(msg_doc.creation),
        content_type=msg_doc.content_type or "Text",
    )

    if not _conversation_allows_autopilot(conversation):
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="conversation_not_open",
            total_sec=elapsed(total_started),
        )
        return

    if _already_replied_to_inbound(conversation, message_id):
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="already_replied",
            total_sec=elapsed(total_started),
        )
        return

    stop_decision = evaluate_stop_rule(conversation, message_id)
    if stop_decision.action == "skip":
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason=stop_decision.reason or "conversation_stopped",
            total_sec=elapsed(total_started),
        )
        return
    if stop_decision.action == "send_final_warning":
        _deliver_ai_reply(conversation, FINAL_WARNING_MESSAGE)
        _log_ai_timing(
            "total_done",
            message=message_id,
            conversation=conversation,
            mode="final_warning",
            total_sec=elapsed(total_started),
        )
        return
    if stop_decision.action == "send_stop":
        _deliver_ai_reply(conversation, STOP_MESSAGE)
        mark_conversation_stopped(conversation)
        frappe.db.commit()
        _log_ai_timing(
            "total_done",
            message=message_id,
            conversation=conversation,
            mode="conversation_stopped",
            total_sec=elapsed(total_started),
        )
        return

    from wa_chat_hub.messaging.windows import evaluate_send_permission

    window_decision = evaluate_send_permission(conversation, "Text")
    if not window_decision.can_send_free_form:
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="messaging_window_closed",
            total_sec=elapsed(total_started),
        )
        frappe.log_error(
            f"Conversation {conversation}: {window_decision.reason}",
            "WA AI Autopilot Skipped (Messaging Window Closed)",
        )
        return

    settings = frappe.get_single("WA Chat Hub Settings")
    body_text = str(msg_doc.body or "").strip()
    if is_delivery_status_query(body_text):
        result = build_delivery_status_reply(conversation, body_text)
        response_text = result.reply
        if _should_auto_send(settings):
            _deliver_ai_reply(conversation, response_text)
            mode = "auto_send"
        else:
            create_ai_suggestion(conversation, "Reply Draft", response_text)
            frappe.db.commit()
            mode = "draft"
        task_log(
            "delivery_status",
            "reply_done",
            conversation=conversation,
            message=message_id,
            mode=mode,
            patient=result.patient,
            encounter=result.encounter,
            shipment=result.shipment,
            awb=result.awb,
            parsed=1 if result.parsed else 0,
        )
        _log_ai_timing(
            "total_done",
            message=message_id,
            conversation=conversation,
            mode=f"delivery_status_{mode}",
            total_sec=elapsed(total_started),
        )
        return

    if is_clinical_history_query(body_text):
        result = build_clinical_history_reply(conversation, body_text)
        response_text = result.reply
        if _should_auto_send(settings):
            _deliver_ai_reply(conversation, response_text)
            mode = "auto_send"
        else:
            create_ai_suggestion(conversation, "Reply Draft", response_text)
            frappe.db.commit()
            mode = "draft"
        task_log(
            "clinical_history",
            "reply_delivered",
            conversation=conversation,
            message=message_id,
            mode=mode,
            patient=result.patient,
            encounters=",".join(result.encounters or []),
            medication_focused=1 if result.medication_focused else 0,
        )
        _log_ai_timing(
            "total_done",
            message=message_id,
            conversation=conversation,
            mode=f"clinical_history_{mode}",
            total_sec=elapsed(total_started),
        )
        return

    context_started = time.monotonic()
    history = _load_recent_conversation_history(conversation)
    history_before_current = [row for row in history if str(row.name) != str(message_id)]

    conversation_context = frappe.db.get_value(
        "Chat Conversation",
        conversation,
        ["channel_account", "department"],
        as_dict=True,
    ) or {}
    channel_account = conversation_context.get("channel_account")
    department = conversation_context.get("department")
    prompt_config = get_effective_prompt_config(channel_account)

    content_type = str(msg_doc.content_type or "Text").title()
    media_url = str(msg_doc.media_url or "").strip()

    media_context = ""
    use_vision_for_image = media_url and content_type == "Image"
    if media_url and content_type in MEDIA_CONTENT_TYPES:
        try:
            if use_vision_for_image:
                caption = _meaningful_body(body_text, content_type)
                media_context = "Customer sent an image on WhatsApp."
                if caption:
                    media_context += f" Caption: {caption}"
            elif content_type in TRANSCRIPT_CONTENT_TYPES:
                media_context = build_transcript_context_for_chat(media_url, content_type, body_text)
            else:
                media_context = build_media_context_for_chat(media_url, content_type, body_text)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Media Context Failed")
            media_context = f"Customer sent a {content_type} attachment."

    last_user_query = _meaningful_body(body_text, content_type) or media_context[:500]

    system_prompt = build_system_prompt_from_config(prompt_config)
    if not system_prompt.strip():
        frappe.log_error(
            "Autopilot skipped: System Prompt is empty in WA Chat Hub Settings "
            f"(channel account: {channel_account or 'global'}).",
            "WA AI Autopilot Config",
        )
        return

    if media_context:
        system_prompt = f"{system_prompt}\n\n{media_context}"

    clinical_history_context = ""
    if last_user_query:
        try:
            clinical_history_context = build_clinical_history_context(conversation, last_user_query)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Clinical History Context Failed")
            clinical_history_context = ""
    if clinical_history_context:
        system_prompt = f"{system_prompt}\n\n{clinical_history_context}"

    kb_result_count = 0
    if last_user_query:
        try:
            kb_results = search_knowledge_base(
                last_user_query,
                top_k=3,
                department=department,
                channel_account=channel_account,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Knowledge Search Failed")
            kb_results = []
        kb_result_count = len(kb_results)
        if kb_results:
            kb_blocks = [
                f"--- {kb['title']} ---\n{kb['content']}"
                for kb in kb_results
                if kb.get("content")
            ]
            if kb_blocks:
                system_prompt = f"{system_prompt}\n\n" + "\n\n".join(kb_blocks)

    multilingual_policy = get_multilingual_policy(prompt_config, settings)
    if multilingual_policy:
        system_prompt = f"{system_prompt}\n\n{multilingual_policy}"

    latest_user_text = _build_latest_user_turn(msg_doc, media_context, use_vision_for_image)

    current_inbound = None
    if use_vision_for_image:
        current_inbound = {
            "media_url": media_url,
            "prompt": latest_user_text or media_context or "",
        }

    _log_ai_timing(
        "context_ready",
        message=message_id,
        conversation=conversation,
        duration_sec=elapsed(context_started),
        history_count=len(history_before_current),
        kb_results=kb_result_count,
        has_media=1 if media_context else 0,
        has_clinical_history=1 if clinical_history_context else 0,
        vision=1 if use_vision_for_image else 0,
    )

    providers = _load_providers()
    if not providers:
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="no_active_provider",
            total_sec=elapsed(total_started),
        )
        frappe.log_error("No active WA LLM Providers found.", "WA AI Bot Error")
        return

    auto_send = _should_auto_send(settings)

    for provider in providers:
        provider_started = time.monotonic()
        _log_ai_timing(
            "provider_start",
            message=message_id,
            conversation=conversation,
            provider=provider.name,
            provider_type=provider.provider_type,
            model=provider.model_name,
        )
        try:
            response_text = call_provider(
                provider,
                system_prompt,
                history_before_current,
                latest_user_text=latest_user_text,
                current_inbound=current_inbound,
            )
            if not response_text or not str(response_text).strip():
                _log_ai_timing(
                    "provider_empty",
                    message=message_id,
                    conversation=conversation,
                    provider=provider.name,
                    model=provider.model_name,
                    duration_sec=elapsed(provider_started),
                )
                continue

            _log_ai_timing(
                "provider_done",
                message=message_id,
                conversation=conversation,
                provider=provider.name,
                model=provider.model_name,
                duration_sec=elapsed(provider_started),
            )
            response_text = _polish_autopilot_reply(str(response_text).strip())

            if auto_send:
                _deliver_ai_reply(conversation, response_text)
            else:
                create_ai_suggestion(conversation, "Reply Draft", str(response_text).strip())
                frappe.db.commit()

            _log_ai_timing(
                "total_done",
                message=message_id,
                conversation=conversation,
                mode="auto_send" if auto_send else "draft",
                total_sec=elapsed(total_started),
            )
            return
        except Exception as e:
            _log_ai_timing(
                "provider_failed",
                message=message_id,
                conversation=conversation,
                provider=provider.name,
                model=provider.model_name,
                duration_sec=elapsed(provider_started),
                error=str(e)[:140],
            )
            frappe.log_error(
                f"LLM Provider {provider.name} failed: {str(e)}",
                "WA AI Fallback Warning",
            )
            continue

    frappe.log_error(
        f"All LLM Providers failed for conversation {conversation}.",
        "WA AI Fatal Error",
    )
    _log_ai_timing(
        "total_failed",
        message=message_id,
        conversation=conversation,
        total_sec=elapsed(total_started),
    )


def _should_auto_send(settings) -> bool:
    return (settings.autopilot_mode or "") == "Limited Auto Reply"


def _inbound_triggers_autopilot(doc) -> bool:
    content_type = str(getattr(doc, "content_type", None) or "Text").title()
    if content_type in MEDIA_CONTENT_TYPES and str(getattr(doc, "media_url", None) or "").strip():
        return True
    return bool(_meaningful_body(str(getattr(doc, "body", None) or ""), content_type))


def _meaningful_body(body: str, content_type: str = "Text") -> str:
    text = str(body or "").strip()
    if not text:
        return ""
    normalized = text.lower()
    if content_type.title() in MEDIA_CONTENT_TYPES and normalized in GENERIC_MEDIA_BODIES:
        return ""
    if normalized in GENERIC_MEDIA_BODIES:
        return ""
    return text


def _load_recent_conversation_history(conversation: str):
    rows = frappe.get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["name", "direction", "body", "content_type", "media_url"],
        order_by="creation desc, name desc",
        limit=CONVERSATION_HISTORY_LIMIT,
    )
    return list(reversed(rows))


def _format_history_line(row) -> str:
    content_type = str(row.content_type or "Text").title()
    body = _meaningful_body(str(row.body or ""), content_type)
    if body:
        return body
    if str(row.media_url or "").strip() and content_type in MEDIA_CONTENT_TYPES:
        return f"[sent {content_type}]"
    return ""


def _build_latest_user_turn(msg_doc, media_context: str, skip_text: bool) -> str:
    """Always pass the triggering inbound message as the final user turn."""
    if skip_text:
        return ""
    body = _meaningful_body(str(msg_doc.body or ""), str(msg_doc.content_type or "Text").title())
    if body:
        return body
    if media_context:
        return media_context
    return ""


def _polish_autopilot_reply(text: str) -> str:
    """Strip role prefixes and overly templated openings from outbound text."""
    cleaned = text.strip()
    cleaned = re.sub(
        r"^(Agent|Assistant|AI|Bot|Customer|You)\s*:\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    boilerplate_starts = (
        "thank you for contacting",
        "thanks for contacting",
        "thank you for reaching out",
        "namaste! thank you",
    )
    lower = cleaned.lower()
    for prefix in boilerplate_starts:
        if lower.startswith(prefix):
            parts = cleaned.split("\n\n", 1)
            if len(parts) > 1 and len(parts[1]) > 20:
                cleaned = parts[1].strip()
            break
    return cleaned


def _conversation_allows_autopilot(conversation: str) -> bool:
    status = frappe.db.get_value("Chat Conversation", conversation, "status")
    return status in (None, "", "Open")


def _already_replied_to_inbound(conversation: str, inbound_message_id: str) -> bool:
    """Only skip duplicate work for the same inbound message, not the whole conversation."""
    inbound_creation = frappe.db.get_value("Chat Message", inbound_message_id, "creation")
    if not inbound_creation:
        return False

    prior_ai = frappe.db.sql(
        """
        SELECT delivery_status
        FROM `tabChat Message`
        WHERE conversation = %s
          AND direction = 'Outbound'
          AND sender_type = 'AI'
          AND creation > %s
        ORDER BY creation ASC
        LIMIT 1
        """,
        (conversation, inbound_creation),
        as_dict=True,
    )
    if not prior_ai:
        return False
    if (prior_ai[0].delivery_status or "") in ("Failed", "Pending"):
        return False
    return True


def _load_providers():
    rows = get_active_llm_provider_rows(CHAT_CAPABILITY)
    providers = []
    for row in rows:
        doc = frappe.get_doc("WA LLM Provider", row.name)
        api_key = doc.get_password("api_key")
        if not api_key:
            continue
        providers.append(
            SimpleNamespace(
                name=row.name,
                provider_type=row.provider_type,
                model_name=row.model_name,
                base_url=row.base_url,
                api_key=api_key,
            )
        )
    return providers


def _deliver_ai_reply(conversation: str, response_text: str) -> None:
    send_started = time.monotonic()
    convo = frappe.get_doc("Chat Conversation", conversation)
    phone_number = frappe.db.get_value("Chat Contact", convo.contact, "phone_number")

    delivery_status = "Sent"
    channel_message_id = None
    outbound = {}

    try:
        _log_ai_timing("send_start", conversation=conversation, channel_account=convo.channel_account)
        outbound = send_outbound_message(conversation, response_text, "Text")
        delivery_status = outbound.get("delivery_status") or "Sent"
        channel_message_id = outbound.get("provider_message_id")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA AI Autopilot Send Failed")
        delivery_status = "Failed"
        outbound = {"sent": False, "error": "Interakt send failed"}

    frappe.flags.wa_ai_outbound_reply = True
    frappe.local.wa_ai_outbound_reply = True
    try:
        append_message(
            {
                "channel_account": convo.channel_account,
                "phone_number": phone_number,
                "direction": "Outbound",
                "sender_type": "AI",
                "content_type": "Text",
                "body": response_text,
                "delivery_status": delivery_status,
                "channel_message_id": channel_message_id,
                "raw_transport_payload": {**outbound, "source": "ai_autopilot"},
            }
        )
    finally:
        frappe.flags.wa_ai_outbound_reply = False
        frappe.local.wa_ai_outbound_reply = False
    frappe.db.commit()
    _log_ai_timing(
        "send_done",
        conversation=conversation,
        delivery_status=delivery_status,
        duration_sec=elapsed(send_started),
    )


def call_provider(provider, system_prompt, history, latest_user_text=None, current_inbound=None):
    messages = [{"role": "system", "content": system_prompt}]
    for h in history:
        role = "user" if h.direction == "Inbound" else "assistant"
        line = _format_history_line(h)
        if line:
            messages.append({"role": role, "content": line})

    if current_inbound and current_inbound.get("media_url"):
        vision_text = (current_inbound.get("prompt") or "").strip()
        user_content = [{"type": "image_url", "image_url": {"url": current_inbound["media_url"]}}]
        if vision_text:
            user_content.insert(0, {"type": "text", "text": vision_text})
        messages.append({"role": "user", "content": user_content})
    elif latest_user_text:
        messages.append({"role": "user", "content": latest_user_text})

    if provider.provider_type in ("OpenAI", "Custom"):
        return call_openai_format(provider, messages, timeout=45)
    if provider.provider_type == "Gemini":
        if not provider.base_url:
            provider.base_url = (
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            )
        return call_openai_format(provider, messages, timeout=45)
    if provider.provider_type == "Anthropic":
        raise Exception(
            "Anthropic specific MCP format requires SDK. Please use OpenAI/Gemini/Custom."
        )

    raise Exception(f"Unsupported provider type {provider.provider_type}")


def fetch_mcp_tools():
    settings = frappe.get_single("WA Chat Hub Settings")
    if not getattr(settings, "allow_mcp_access", 0):
        return []
    if not frappe.db.exists("DocType", "WA MCP Tool Endpoint"):
        return []

    tools_docs = frappe.get_all(
        "WA MCP Tool Endpoint",
        filters={"is_active": 1},
        fields=["tool_name", "description", "parameters_schema", "endpoint_url", "http_method"],
    )
    tools = []
    for t in tools_docs:
        try:
            params = (
                json.loads(t.parameters_schema)
                if t.parameters_schema
                else {"type": "object", "properties": {}}
            )
        except Exception:
            params = {"type": "object", "properties": {}}

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": t.tool_name,
                    "description": t.description or "No description",
                    "parameters": params,
                },
                "_meta": {
                    "url": t.endpoint_url,
                    "method": t.http_method,
                },
            }
        )
    return tools


def execute_mcp_tool(tool_name, arguments_dict):
    tools = fetch_mcp_tools()
    tool_meta = next((t["_meta"] for t in tools if t["function"]["name"] == tool_name), None)
    if not tool_meta:
        return f"Error: Tool {tool_name} not found."

    try:
        url = tool_meta["url"]

        if url.startswith("http"):
            if tool_meta["method"] == "POST":
                resp = requests.post(url, json=arguments_dict, timeout=10)
            else:
                resp = requests.get(url, params=arguments_dict, timeout=10)
            return resp.text

        fn = frappe.get_attr(url)
        res = fn(**arguments_dict)
        return json.dumps(res)
    except Exception as e:
        return f"Error executing {tool_name}: {str(e)}"


def _max_tokens_payload_key(model_name: str | None) -> str:
    model = str(model_name or "").strip().lower()
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


def call_openai_format(provider, messages, timeout=20):
    request_started = time.monotonic()
    url = provider.base_url or "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }

    if url.endswith("/") and "chat/completions" not in url:
        url += "chat/completions"

    tools = fetch_mcp_tools()
    api_tools = [{"type": t["type"], "function": t["function"]} for t in tools] if tools else None
    token_limit_key = _max_tokens_payload_key(provider.model_name)

    payload = {
        "model": provider.model_name,
        "messages": messages,
        "temperature": 0.75,
        "presence_penalty": 0.4,
        "frequency_penalty": 0.3,
        token_limit_key: 500,
    }
    if api_tools:
        payload["tools"] = api_tools

    _log_ai_timing(
        "api_request_start",
        provider=provider.name,
        provider_type=provider.provider_type,
        model=provider.model_name,
        message_count=len(messages),
        tools=1 if api_tools else 0,
        token_limit_key=token_limit_key,
        timeout_sec=timeout,
    )
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    _log_ai_timing(
        "api_request_done",
        provider=provider.name,
        model=provider.model_name,
        status_code=resp.status_code,
        duration_sec=elapsed(request_started),
    )

    if resp.status_code != 200:
        frappe.log_error(
            f"API Error {resp.status_code}: {resp.text}",
            "WA AI Provider API Failure",
        )
        resp.raise_for_status()

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        frappe.log_error(
            f"OpenAI empty choices for model {provider.model_name}: {resp.text[:500]}",
            "WA AI Provider API Failure",
        )
        return ""

    message = choices[0].get("message") or {}

    if message.get("tool_calls"):
        messages.append(message)

        for tc in message["tool_calls"]:
            tool_started = time.monotonic()
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}
            tool_res = execute_mcp_tool(tc["function"]["name"], args)
            _log_ai_timing(
                "tool_done",
                provider=provider.name,
                model=provider.model_name,
                tool=tc["function"]["name"],
                duration_sec=elapsed(tool_started),
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["function"]["name"],
                    "content": str(tool_res),
                }
            )

        payload["messages"] = messages
        followup_started = time.monotonic()
        _log_ai_timing(
            "api_followup_start",
            provider=provider.name,
            model=provider.model_name,
            message_count=len(messages),
        )
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        _log_ai_timing(
            "api_followup_done",
            provider=provider.name,
            model=provider.model_name,
            status_code=resp.status_code,
            duration_sec=elapsed(followup_started),
        )
        resp.raise_for_status()
        data = resp.json()
        follow_choices = data.get("choices") or []
        if not follow_choices:
            return ""
        return (follow_choices[0].get("message") or {}).get("content", "") or ""

    return message.get("content", "") or ""
