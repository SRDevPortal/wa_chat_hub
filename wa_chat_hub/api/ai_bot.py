import json
import re
from types import SimpleNamespace

import frappe
import requests
from frappe.utils import cint
from frappe.utils.background_jobs import enqueue

from wa_chat_hub.ai.ocr_summary import GENERIC_MEDIA_BODIES, build_media_context_for_chat
from wa_chat_hub.ai.service import create_ai_suggestion
from wa_chat_hub.api.vector_search import search_knowledge_base
from wa_chat_hub.outbound import send_outbound_message
from wa_chat_hub.prompts import (
    build_system_prompt_from_config,
    get_effective_prompt_config,
    get_multilingual_policy,
)
from wa_chat_hub.services import append_message

CONVERSATION_HISTORY_LIMIT = 40
MEDIA_CONTENT_TYPES = frozenset({"Image", "Video", "Audio", "Document", "Sticker"})


def on_message_received(doc, method):
    if doc.direction != "Inbound":
        return

    if not _inbound_triggers_autopilot(doc):
        return

    settings = frappe.get_single("WA Chat Hub Settings")
    if not settings.enable_ai_autopilot:
        return
    if (settings.autopilot_mode or "Suggest Only") == "Disabled":
        return

    # Run after commit so the worker can load the Chat Message row.
    # In developer_mode, process inline (now=True) so a worker is not required locally.
    enqueue(
        "wa_chat_hub.api.ai_bot.process_message",
        queue="short",
        message_id=doc.name,
        enqueue_after_commit=True,
        now=bool(frappe.conf.get("developer_mode")),
        job_id=f"wa_ai_autopilot_{doc.name}",
        deduplicate=True,
    )


def process_message(message_id):
    frappe.set_user("Administrator")

    msg_doc = frappe.get_doc("Chat Message", message_id)
    conversation = msg_doc.conversation

    if not _conversation_allows_autopilot(conversation):
        return

    if _already_replied_to_inbound(conversation, message_id):
        return

    from wa_chat_hub.messaging.windows import evaluate_send_permission

    window_decision = evaluate_send_permission(conversation, "Text")
    if not window_decision.can_send_free_form:
        frappe.log_error(
            f"Conversation {conversation}: {window_decision.reason}",
            "WA AI Autopilot Skipped (Messaging Window Closed)",
        )
        return

    history = frappe.get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["name", "direction", "body", "content_type", "media_url"],
        order_by="creation asc",
        limit=CONVERSATION_HISTORY_LIMIT,
    )
    history_before_current = [row for row in history if str(row.name) != str(message_id)]

    settings = frappe.get_single("WA Chat Hub Settings")
    channel_account = frappe.db.get_value("Chat Conversation", conversation, "channel_account")
    prompt_config = get_effective_prompt_config(channel_account)

    content_type = str(msg_doc.content_type or "Text").title()
    media_url = str(msg_doc.media_url or "").strip()
    body_text = str(msg_doc.body or "").strip()

    media_context = ""
    use_vision_for_image = media_url and content_type == "Image"
    if media_url and content_type in MEDIA_CONTENT_TYPES:
        try:
            if use_vision_for_image:
                caption = _meaningful_body(body_text, content_type)
                media_context = "Customer sent an image on WhatsApp."
                if caption:
                    media_context += f" Caption: {caption}"
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

    if last_user_query:
        kb_results = search_knowledge_base(last_user_query, top_k=3)
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

    providers = _load_providers()
    if not providers:
        frappe.log_error("No active WA LLM Providers found.", "WA AI Bot Error")
        return

    auto_send = _should_auto_send(settings)

    for provider in providers:
        try:
            response_text = call_provider(
                provider,
                system_prompt,
                history_before_current,
                latest_user_text=latest_user_text,
                current_inbound=current_inbound,
            )
            if not response_text or not str(response_text).strip():
                continue

            response_text = _polish_autopilot_reply(str(response_text).strip())

            if auto_send:
                _deliver_ai_reply(conversation, response_text)
            else:
                create_ai_suggestion(conversation, "Reply Draft", str(response_text).strip())
                frappe.db.commit()

            return
        except Exception as e:
            frappe.log_error(
                f"LLM Provider {provider.name} failed: {str(e)}",
                "WA AI Fallback Warning",
            )
            continue

    frappe.log_error(
        f"All LLM Providers failed for conversation {conversation}.",
        "WA AI Fatal Error",
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
          AND creation >= %s
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
    rows = frappe.get_all(
        "WA LLM Provider",
        filters={"is_active": 1},
        fields=["name", "provider_type", "model_name", "base_url"],
        order_by="priority asc",
    )
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
    convo = frappe.get_doc("Chat Conversation", conversation)
    phone_number = frappe.db.get_value("Chat Contact", convo.contact, "phone_number")

    delivery_status = "Sent"
    channel_message_id = None
    outbound = {}

    try:
        outbound = send_outbound_message(conversation, response_text, "Text")
        delivery_status = outbound.get("delivery_status") or "Sent"
        channel_message_id = outbound.get("provider_message_id")
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA AI Autopilot Send Failed")
        delivery_status = "Failed"
        outbound = {"sent": False, "error": "Interakt send failed"}

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
    frappe.db.commit()


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
    if not settings.allow_mcp_access:
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


def call_openai_format(provider, messages, timeout=20):
    url = provider.base_url or "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }

    if url.endswith("/") and "chat/completions" not in url:
        url += "chat/completions"

    tools = fetch_mcp_tools()
    api_tools = [{"type": t["type"], "function": t["function"]} for t in tools] if tools else None

    payload = {
        "model": provider.model_name,
        "messages": messages,
        "temperature": 0.75,
        "presence_penalty": 0.4,
        "frequency_penalty": 0.3,
        "max_tokens": 500,
    }
    if api_tools:
        payload["tools"] = api_tools

    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)

    if resp.status_code != 200:
        frappe.log_error(
            f"API Error {resp.status_code}: {resp.text}",
            "WA AI Provider API Failure",
        )
        resp.raise_for_status()

    data = resp.json()
    message = data["choices"][0]["message"]

    if message.get("tool_calls"):
        messages.append(message)

        for tc in message["tool_calls"]:
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}
            tool_res = execute_mcp_tool(tc["function"]["name"], args)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": tc["function"]["name"],
                    "content": str(tool_res),
                }
            )

        payload["messages"] = messages
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"].get("content", "")

    return message.get("content", "")
