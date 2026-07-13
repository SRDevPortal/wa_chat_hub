import json
import hashlib
import re
import time
from difflib import SequenceMatcher
from types import SimpleNamespace

import frappe
import requests
from frappe.utils import add_to_date, cint, get_datetime
from frappe.utils.background_jobs import enqueue
from frappe.utils.synchronization import filelock

from wa_chat_hub.ai.ocr_summary import (
    GENERIC_MEDIA_BODIES,
    build_media_context_for_chat,
)
from wa_chat_hub.ai.providers import CHAT_CAPABILITY, get_active_llm_provider_rows
from wa_chat_hub.ai.media_transcription import (
    TRANSCRIPT_CONTENT_TYPES,
    build_transcript_context_for_chat,
)
from wa_chat_hub.ai.delivery_status import build_delivery_status_reply, is_delivery_status_query
from wa_chat_hub.ai.service import create_ai_suggestion
from wa_chat_hub.api.vector_search import search_knowledge_base
from wa_chat_hub.agent_router import build_agent_prompt, persist_agent_route, resolve_agent_route
from wa_chat_hub.outbound import send_outbound_message
from wa_chat_hub.prompts import (
    build_system_prompt_from_config,
    get_effective_prompt_config,
    get_multilingual_policy,
)
from wa_chat_hub.services import append_message
from wa_chat_hub.security import (
    assert_ai_doctype_permission,
    safe_ai_exists,
    safe_ai_get_all,
    safe_ai_get_doc,
    safe_ai_get_value,
    set_ai_security_context,
    set_service_user_context,
)
from wa_chat_hub.task_logger import elapsed, queue_wait_seconds, task_log

CONVERSATION_HISTORY_LIMIT = 100
MAX_HISTORY_MESSAGE_CHARS = 1200
MEDIA_CONTENT_TYPES = frozenset({"Image", "Video", "Audio", "Document", "Sticker"})
DEFAULT_TEXT_AUTOPILOT_REPLY_DELAY_SECONDS = 3
DEFAULT_MEDIA_AUTOPILOT_REPLY_DELAY_SECONDS = 15
AUTOPILOT_BATCH_LOCK_TIMEOUT = 180
AUTOPILOT_BATCH_LOOKBACK_MINUTES = 15
AUTOPILOT_MEDIA_BURST_LOOKBACK_MINUTES = 10
AUTOPILOT_MEDIA_BURST_GAP_SECONDS = 90
AUTOPILOT_MEDIA_SETTLE_SECONDS = 5
LOW_CONTEXT_INPUT_CHAR_BUDGET = 6500
LOW_CONTEXT_SYSTEM_CHAR_BUDGET = 4200


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
    if not message_name or not safe_ai_exists("Chat Message", message_name):
        return

    doc = safe_ai_get_doc("Chat Message", message_name)
    if doc.direction != "Inbound":
        return
    if not _inbound_triggers_autopilot(doc):
        return

    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    if not settings.enable_ai_autopilot:
        return
    if (settings.autopilot_mode or "Suggest Only") == "Disabled":
        return

    try:
        if _autopilot_batching_enabled(settings):
            enqueue(
                "wa_chat_hub.api.ai_bot.process_conversation",
                queue="short",
                conversation=doc.conversation,
                trigger_message_id=message_name,
                enqueue_after_commit=True,
                now=False,
                job_id=f"wa_ai_autopilot_conversation_{doc.conversation}",
                deduplicate=True,
            )
            _log_ai_timing(
                "enqueue",
                message=message_name,
                conversation=getattr(doc, "conversation", None),
                content_type=getattr(doc, "content_type", None) or "Text",
                queue="short",
                mode="conversation_batch",
            )
            return

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


def process_conversation(conversation: str, trigger_message_id: str | None = None):
    total_started = time.monotonic()
    set_service_user_context("ai_autopilot")
    set_ai_security_context(
        operation="ai_autopilot_batch",
        conversation=conversation,
        message=str(trigger_message_id or ""),
    )

    if not conversation:
        return

    if not _conversation_allows_autopilot(conversation):
        _log_ai_timing(
            "skip",
            message=trigger_message_id,
            conversation=conversation,
            reason="conversation_not_open",
            total_sec=elapsed(total_started),
        )
        return

    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    if not settings.enable_ai_autopilot:
        return
    if (settings.autopilot_mode or "Suggest Only") == "Disabled":
        return

    _, total_wait = _wait_for_conversation_batch_window(conversation, settings)

    with filelock(_autopilot_batch_lock_name(conversation), timeout=AUTOPILOT_BATCH_LOCK_TIMEOUT):
        msg_doc = _latest_autopilot_inbound(conversation)
        if not msg_doc:
            _log_ai_timing(
                "skip",
                message=trigger_message_id,
                conversation=conversation,
                reason="no_inbound_for_batch",
                total_sec=elapsed(total_started),
            )
            return

        if _autopilot_batch_already_answered(conversation, msg_doc):
            _log_ai_timing(
                "skip",
                message=msg_doc.name,
                trigger_message=trigger_message_id,
                conversation=conversation,
                reason="batch_already_answered",
                waited_sec=total_wait,
                total_sec=elapsed(total_started),
            )
            return

        if trigger_message_id and str(msg_doc.name) != str(trigger_message_id):
            _log_ai_timing(
                "batch_selected_latest",
                message=msg_doc.name,
                trigger_message=trigger_message_id,
                conversation=conversation,
                waited_sec=total_wait,
            )

        return process_message(msg_doc.name, skip_batch_wait=True)


def process_message(message_id, skip_batch_wait: bool = False):
    total_started = time.monotonic()
    set_service_user_context("ai_autopilot")

    msg_doc = safe_ai_get_doc("Chat Message", message_id)
    frappe.flags.wa_ai_reply_to_message = str(message_id)
    frappe.local.wa_ai_reply_to_message = str(message_id)
    conversation = msg_doc.conversation
    set_ai_security_context(
        operation="ai_autopilot",
        conversation=conversation,
        message=str(message_id),
    )
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

    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    if not skip_batch_wait:
        if _autopilot_batching_enabled(settings):
            return process_conversation(conversation, trigger_message_id=message_id)
        superseded, delay_seconds = _wait_for_autopilot_batch_window(msg_doc, settings)
        if superseded:
            _log_ai_timing(
                "skip",
                message=message_id,
                conversation=conversation,
                reason="superseded_by_newer_inbound",
                delay_seconds=delay_seconds,
                content_type=msg_doc.content_type or "Text",
                total_sec=elapsed(total_started),
            )
            return

    body_text = str(msg_doc.body or "").strip()
    content_type = str(msg_doc.content_type or "Text").title()
    media_url = str(msg_doc.media_url or "").strip()

    if is_delivery_status_query(body_text):
        result = build_delivery_status_reply(conversation, body_text)
        response_text = result.reply
        mode = _deliver_or_draft_ai_reply(conversation, response_text, settings, message_id) or "duplicate_skip"
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

    context_started = time.monotonic()
    history = _load_recent_conversation_history(conversation)
    history_before_current = [row for row in history if str(row.name) != str(message_id)]

    conversation_context = safe_ai_get_value(
        "Chat Conversation",
        conversation,
        ["channel_account", "department"],
        as_dict=True,
    ) or {}
    channel_account = conversation_context.get("channel_account")
    department = conversation_context.get("department")
    set_ai_security_context(channel_account=channel_account)
    prompt_config = get_effective_prompt_config(channel_account)
    route = resolve_agent_route(conversation)
    persist_agent_route(conversation, route)
    if route.auto_reply_mode:
        settings.autopilot_mode = route.auto_reply_mode

    media_context = ""
    use_vision_for_image = False
    if media_url and content_type in MEDIA_CONTENT_TYPES:
        try:
            media_context = _build_recent_media_batch_context(conversation, msg_doc)
            if not media_context:
                if content_type in TRANSCRIPT_CONTENT_TYPES:
                    media_context = build_transcript_context_for_chat(media_url, content_type, body_text)
                else:
                    media_context = build_media_context_for_chat(media_url, content_type, body_text)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Media Context Failed")
            media_context = f"Customer sent a {content_type} attachment."
    elif _looks_like_recent_attachment_followup(body_text):
        try:
            media_context = _build_recent_attachment_followup_context(conversation, msg_doc)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Recent Attachment Context Failed")
            media_context = ""

    last_user_query = _meaningful_body(body_text, content_type) or media_context[:500]

    agent_prompt = build_agent_prompt(route)
    if route.agent_profile and agent_prompt:
        agent_prompt_config = SimpleNamespace(
            system_prompt=route.system_prompt,
            medical_guardrail_policy=route.medical_guardrail_policy,
            escalation_policy=route.escalation_policy,
        )
        system_prompt = build_system_prompt_from_config(agent_prompt_config)
        if route.prompt_overlay:
            system_prompt = f"{system_prompt}\n\n{route.prompt_overlay}"
    else:
        system_prompt = build_system_prompt_from_config(prompt_config)
    if not system_prompt.strip():
        frappe.log_error(
            "Autopilot skipped: System Prompt is empty in WA Chat Hub Settings "
            f"(channel account: {channel_account or 'global'}).",
            "WA AI Autopilot Config",
        )
        return

    known_context = _build_known_conversation_context(conversation)
    if known_context:
        system_prompt = f"{system_prompt}\n\n{known_context}"

    if media_context:
        system_prompt = f"{system_prompt}\n\n{media_context}"

    kb_result_count = 0
    if last_user_query:
        try:
            kb_results = search_knowledge_base(
                last_user_query,
                top_k=3,
                department=route.department,
                channel_account=channel_account,
                allowed_names=route.allowed_knowledge_bases or None,
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
        vision=1 if use_vision_for_image else 0,
    )

    providers = _load_providers(route.llm_provider)
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
                allowed_tool_names=route.allowed_tool_names,
                max_tool_calls=route.max_tool_calls,
                tool_context={
                    "conversation": conversation,
                    "patient": route.patient,
                    "agent_profile": route.agent_profile,
                    "department": route.department,
                    "identity_status": route.identity_status,
                },
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
            if _looks_like_degenerate_reply(response_text):
                _log_ai_timing(
                    "provider_bad_output",
                    message=message_id,
                    conversation=conversation,
                    provider=provider.name,
                    model=provider.model_name,
                    reason="degenerate_repetition",
                    duration_sec=elapsed(provider_started),
                )
                _safe_log_error(
                    "WA AI Provider Bad Output",
                    f"Rejected repetitive provider reply for message {message_id}: {response_text[:500]}",
                )
                continue

            delivered_mode = _deliver_or_draft_ai_reply(conversation, response_text, settings, message_id)
            if not delivered_mode:
                _log_ai_timing(
                    "skip",
                    message=message_id,
                    conversation=conversation,
                    reason="near_duplicate_reply",
                    total_sec=elapsed(total_started),
                )
                return

            _log_ai_timing(
                "total_done",
                message=message_id,
                conversation=conversation,
                mode=delivered_mode,
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
            _safe_log_error(
                "WA AI Fallback Warning",
                f"LLM Provider {provider.name} failed: {str(e)}",
            )
            continue

    _safe_log_error(
        "WA AI Fatal Error",
        f"All LLM Providers failed for conversation {conversation}.",
    )
    _log_ai_timing(
        "total_failed",
        message=message_id,
        conversation=conversation,
        total_sec=elapsed(total_started),
    )


def _should_auto_send(settings) -> bool:
    return (settings.autopilot_mode or "") == "Limited Auto Reply"


def _deliver_or_draft_ai_reply(
    conversation: str,
    response_text: str,
    settings,
    message_id: str | None = None,
) -> str:
    response_text = str(response_text or "").strip()
    if not response_text:
        return ""
    if _looks_like_recent_duplicate_reply(conversation, response_text):
        _log_ai_timing(
            "skip",
            message=message_id,
            conversation=conversation,
            reason="near_duplicate_reply",
        )
        return ""
    if _should_auto_send(settings):
        _deliver_ai_reply(conversation, response_text)
        return "auto_send"
    create_ai_suggestion(conversation, "Reply Draft", response_text)
    frappe.db.commit()
    return "draft"


def _normalize_reply_for_similarity(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "").strip().lower())
    text = re.sub(r"[^\w\s\u0900-\u097F]", "", text)
    return text.strip()


def _near_duplicate_text(a: str, b: str) -> bool:
    left = _normalize_reply_for_similarity(a)
    right = _normalize_reply_for_similarity(b)
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 35:
        return False
    if left in right or right in left:
        return True
    return SequenceMatcher(None, left, right).ratio() >= 0.92


def _looks_like_recent_duplicate_reply(conversation: str, response_text: str) -> bool:
    rows = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Outbound",
            "sender_type": ["in", ["AI", "Agent"]],
            "delivery_status": ["!=", "Failed"],
        },
        fields=["name", "body", "sender_type"],
        order_by="creation desc, name desc",
        limit=3,
    )
    for row in rows:
        if _near_duplicate_text(response_text, row.get("body") or ""):
            _log_ai_timing(
                "duplicate_reply_detected",
                conversation=conversation,
                prior_message=row.get("name"),
                prior_sender=row.get("sender_type"),
            )
            return True
    return False


def _build_known_conversation_context(conversation: str) -> str:
    fields = [
        "contact",
        "department",
        "assigned_to",
        "linked_crm_lead",
        "linked_reference_doctype",
        "linked_reference_name",
        "lead_score",
        "lead_lan",
        "lead_temperature",
        "ai_summary",
    ]
    convo = safe_ai_get_value("Chat Conversation", conversation, fields, as_dict=True) or {}
    facts = []
    contact_name = convo.get("contact")
    if contact_name:
        contact = safe_ai_get_value(
            "Chat Contact",
            contact_name,
            ["display_name", "phone_number", "linked_lead", "linked_patient"],
            as_dict=True,
        ) or {}
        if contact.get("display_name"):
            facts.append(f"Contact name: {contact.get('display_name')}")
        if contact.get("phone_number"):
            facts.append(f"WhatsApp phone: {contact.get('phone_number')}")
        if contact.get("linked_lead"):
            facts.append(f"Linked Lead: {contact.get('linked_lead')}")
        if contact.get("linked_patient"):
            facts.append(f"Linked Patient: {contact.get('linked_patient')}")

    for label, fieldname in (
        ("Department", "department"),
        ("Assigned to", "assigned_to"),
        ("CRM Lead", "linked_crm_lead"),
        ("Linked reference type", "linked_reference_doctype"),
        ("Linked reference name", "linked_reference_name"),
        ("Lead score", "lead_score"),
        ("Lead language", "lead_lan"),
        ("Lead temperature", "lead_temperature"),
    ):
        value = convo.get(fieldname)
        if value not in (None, ""):
            facts.append(f"{label}: {value}")

    if convo.get("ai_summary"):
        facts.append("Existing conversation summary: " + _truncate_history_text(convo.get("ai_summary"), 1000))

    if not facts:
        return ""
    return (
        "Known conversation facts. Use these as already-known details and do not ask for them again unless "
        "the customer changes or corrects them:\n- "
        + "\n- ".join(str(fact) for fact in facts if fact)
    )


def _extract_context_block(context: str, marker: str) -> str:
    if marker not in context:
        return ""
    tail = context.split(marker, 1)[1].strip()
    if "\nUse " in tail:
        tail = tail.split("\nUse ", 1)[0].strip()
    return tail


def _looks_like_recent_attachment_followup(body: str) -> bool:
    text = str(body or "").strip().lower()
    if not text:
        return False
    keywords = (
        "report",
        "image",
        "photo",
        "pic",
        "test",
        "kft",
        "creatinine",
        "value",
        "values",
        "result",
        "problem kya",
        "kya problem",
        "kya h",
        "kya hai",
        "btaoge",
        "bataoge",
        "explain",
        "read",
        "padh",
    )
    return any(keyword in text for keyword in keywords)


def _build_recent_attachment_followup_context(conversation: str, msg_doc) -> str:
    current_creation = getattr(msg_doc, "creation", None)
    filters = {
        "conversation": conversation,
        "direction": "Inbound",
        "content_type": ["in", ["Image", "Document"]],
        "media_url": ["is", "set"],
    }
    if current_creation:
        filters["creation"] = ["<", current_creation]

    recent = safe_ai_get_all(
        "Chat Message",
        filters=filters,
        fields=["name", "creation", "content_type", "body", "media_url"],
        order_by="creation desc, name desc",
        limit=1,
    )
    if not recent:
        return ""

    row = recent[0]
    context = build_media_context_for_chat(
        row.media_url,
        row.content_type,
        str(row.body or ""),
    )
    if not context:
        return ""
    return (
        "Customer is asking a follow-up question about the most recent image/report attachment. "
        "Use the extracted report text below to answer the customer's question. If values look "
        "abnormal, explain them simply and advise doctor/nephrologist review; do not diagnose or prescribe.\n\n"
        f"Recent attachment: Chat Message {row.name} sent at {row.creation}\n"
        f"{context}"
    )


def _build_recent_media_batch_context(conversation: str, msg_doc) -> str:
    current_creation = getattr(msg_doc, "creation", None)
    if not conversation or not current_creation:
        return ""

    if _is_media_message(msg_doc):
        batch_start = _media_burst_window_start(conversation, current_creation)
        start_operator = ">="
    else:
        batch_start = _autopilot_batch_window_start(conversation, current_creation)
        start_operator = ">"
    filters = [
        ["conversation", "=", conversation],
        ["direction", "=", "Inbound"],
        ["content_type", "in", list(MEDIA_CONTENT_TYPES)],
        ["media_url", "is", "set"],
        ["creation", "<=", current_creation],
        ["creation", start_operator, batch_start],
    ]

    rows = safe_ai_get_all(
        "Chat Message",
        filters=filters,
        fields=["name", "creation", "content_type", "body", "media_url"],
        order_by="creation asc, name asc",
        limit=8,
    )
    if not rows:
        return ""

    blocks = []
    for index, row in enumerate(rows, start=1):
        row_content_type = str(row.content_type or "Text").title()
        row_body = str(row.body or "")
        try:
            if row_content_type in TRANSCRIPT_CONTENT_TYPES:
                context = build_transcript_context_for_chat(row.media_url, row_content_type, row_body)
            else:
                context = build_media_context_for_chat(row.media_url, row_content_type, row_body)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA AI Media Batch Context Item Failed")
            context = f"Customer sent a {row_content_type} attachment."
        if context:
            blocks.append(
                f"Attachment {index}: Chat Message {row.name}, type {row_content_type}, sent at {row.creation}\n{context}"
            )

    if not blocks:
        return ""

    if len(blocks) == 1:
        return blocks[0]

    return (
        f"Customer sent {len(blocks)} recent media attachments before this reply. "
        "Review them together and respond once for the combined customer action.\n\n"
        + "\n\n".join(blocks)
    )


def _autopilot_batch_lock_name(conversation: str) -> str:
    digest = hashlib.sha256(str(conversation or "unknown").encode("utf-8")).hexdigest()[:24]
    return f"wa_ai_batch_{digest}"


def _autopilot_batch_window_start(conversation: str, current_creation):
    current_dt = get_datetime(current_creation)
    recent_start = add_to_date(current_dt, minutes=-AUTOPILOT_BATCH_LOOKBACK_MINUTES)
    last_outbound = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Outbound",
            "creation": ["<", current_dt],
        },
        fields=["creation"],
        order_by="creation desc, name desc",
        limit=1,
    )
    if not last_outbound:
        return recent_start

    last_outbound_dt = get_datetime(last_outbound[0].creation)
    return max(recent_start, last_outbound_dt)


def _autopilot_batch_already_answered(conversation: str, msg_doc) -> bool:
    current_creation = getattr(msg_doc, "creation", None)
    if not conversation or not current_creation:
        return False

    if _is_media_message(msg_doc) and _media_burst_already_answered(conversation, msg_doc):
        return True

    batch_start = _autopilot_batch_window_start(conversation, current_creation)
    outbound_rows = safe_ai_get_all(
        "Chat Message",
        filters=[
            ["conversation", "=", conversation],
            ["direction", "=", "Outbound"],
            ["sender_type", "in", ["AI", "Agent"]],
            ["creation", ">", batch_start],
        ],
        fields=["name", "delivery_status", "sender_type", "creation"],
        order_by="creation asc, name asc",
        limit=10,
    )
    for row in outbound_rows:
        if str(row.delivery_status or "") in ("Failed", "Pending"):
            continue
        return True
    return False


def _media_burst_already_answered(conversation: str, msg_doc) -> bool:
    current_creation = getattr(msg_doc, "creation", None)
    if not conversation or not current_creation:
        return False

    current_dt = get_datetime(current_creation)
    burst_start = _media_burst_window_start(conversation, current_creation)
    prior_media = safe_ai_get_all(
        "Chat Message",
        filters=[
            ["conversation", "=", conversation],
            ["direction", "=", "Inbound"],
            ["content_type", "in", list(MEDIA_CONTENT_TYPES)],
            ["media_url", "is", "set"],
            ["creation", ">=", burst_start],
            ["creation", "<", current_dt],
        ],
        fields=["name", "creation"],
        order_by="creation asc, name asc",
        limit=1,
    )
    if not prior_media:
        return False

    outbound_rows = safe_ai_get_all(
        "Chat Message",
        filters=[
            ["conversation", "=", conversation],
            ["direction", "=", "Outbound"],
            ["sender_type", "in", ["AI", "Agent"]],
            ["creation", ">", prior_media[0].creation],
            ["creation", "<=", current_dt],
        ],
        fields=["name", "delivery_status", "sender_type", "creation"],
        order_by="creation asc, name asc",
        limit=10,
    )
    for row in outbound_rows:
        if str(row.delivery_status or "") in ("Failed", "Pending"):
            continue
        return True
    return False


def _media_burst_window_start(conversation: str, current_creation):
    current_dt = get_datetime(current_creation)
    lookback_start = add_to_date(current_dt, minutes=-AUTOPILOT_MEDIA_BURST_LOOKBACK_MINUTES)
    rows = safe_ai_get_all(
        "Chat Message",
        filters=[
            ["conversation", "=", conversation],
            ["direction", "=", "Inbound"],
            ["content_type", "in", list(MEDIA_CONTENT_TYPES)],
            ["media_url", "is", "set"],
            ["creation", ">=", lookback_start],
            ["creation", "<=", current_dt],
        ],
        fields=["name", "creation"],
        order_by="creation desc, name desc",
        limit=20,
    )

    burst_start = current_dt
    previous_dt = current_dt
    for row in rows:
        row_dt = get_datetime(row.creation)
        if (previous_dt - row_dt).total_seconds() > AUTOPILOT_MEDIA_BURST_GAP_SECONDS:
            break
        burst_start = row_dt
        previous_dt = row_dt

    last_outbound = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Outbound",
            "creation": ["<", current_dt],
        },
        fields=["creation"],
        order_by="creation desc, name desc",
        limit=1,
    )
    if last_outbound:
        last_outbound_dt = get_datetime(last_outbound[0].creation)
        if last_outbound_dt > burst_start:
            return last_outbound_dt

    return burst_start


def _doc_value(doc, fieldname: str, default=None):
    if isinstance(doc, dict):
        return doc.get(fieldname, default)
    return getattr(doc, fieldname, default)


def _is_media_message(doc) -> bool:
    content_type = str(_doc_value(doc, "content_type", "Text") or "Text").title()
    media_url = str(_doc_value(doc, "media_url", None) or "").strip()
    return bool(content_type in MEDIA_CONTENT_TYPES and media_url)


def _autopilot_batching_enabled(settings) -> bool:
    return bool(cint(getattr(settings, "enable_autopilot_reply_batching", 1)))


def _autopilot_reply_delay_seconds(doc, settings) -> int:
    if _is_media_message(doc):
        value = getattr(settings, "media_autopilot_reply_delay_seconds", None)
        default = DEFAULT_MEDIA_AUTOPILOT_REPLY_DELAY_SECONDS
    else:
        value = getattr(settings, "text_autopilot_reply_delay_seconds", None)
        default = DEFAULT_TEXT_AUTOPILOT_REPLY_DELAY_SECONDS

    if value in (None, ""):
        return default
    try:
        return max(0, cint(value))
    except Exception:
        return default


def _wait_for_autopilot_batch_window(msg_doc, settings) -> tuple[bool, int]:
    if not _autopilot_batching_enabled(settings):
        return False, 0

    delay_seconds = _autopilot_reply_delay_seconds(msg_doc, settings)
    if delay_seconds and not getattr(frappe.flags, "in_test", False):
        time.sleep(delay_seconds)

    return _newer_autopilot_message_exists(msg_doc), delay_seconds


def _wait_for_conversation_batch_window(conversation: str, settings):
    latest_doc = _latest_autopilot_inbound(conversation)
    if not latest_doc:
        return None, 0

    if not _autopilot_batching_enabled(settings):
        return latest_doc, 0

    total_wait = 0
    for _ in range(4):
        delay_seconds = _autopilot_reply_delay_seconds(latest_doc, settings)
        if delay_seconds and not getattr(frappe.flags, "in_test", False):
            time.sleep(delay_seconds)
            total_wait += delay_seconds

        refreshed_doc = _latest_autopilot_inbound(conversation)
        if not refreshed_doc:
            return latest_doc, total_wait
        if str(refreshed_doc.name) == str(latest_doc.name):
            if (
                _is_media_message(refreshed_doc)
                and AUTOPILOT_MEDIA_SETTLE_SECONDS
                and not getattr(frappe.flags, "in_test", False)
            ):
                time.sleep(AUTOPILOT_MEDIA_SETTLE_SECONDS)
                total_wait += AUTOPILOT_MEDIA_SETTLE_SECONDS
                settled_doc = _latest_autopilot_inbound(conversation)
                if settled_doc and str(settled_doc.name) != str(refreshed_doc.name):
                    latest_doc = settled_doc
                    continue
            return refreshed_doc, total_wait
        latest_doc = refreshed_doc

    return latest_doc, total_wait


def _latest_autopilot_inbound(conversation: str):
    if not conversation:
        return None

    rows = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Inbound",
        },
        fields=["name", "body", "content_type", "media_url", "sender_type"],
        order_by="creation desc, name desc",
        limit=30,
    )
    for row in rows:
        if str(_doc_value(row, "sender_type", "") or "").strip() in ("AI", "System", "Bot"):
            continue
        if _inbound_triggers_autopilot(row):
            return safe_ai_get_doc("Chat Message", row.name)
    return None


def _newer_autopilot_message_exists(msg_doc) -> bool:
    conversation = _doc_value(msg_doc, "conversation")
    current_creation = _doc_value(msg_doc, "creation")
    if not conversation or not current_creation:
        return False

    rows = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Inbound",
            "creation": [">", current_creation],
        },
        fields=["name", "body", "content_type", "media_url", "sender_type"],
        order_by="creation desc, name desc",
        limit=20,
    )
    for row in rows:
        if str(_doc_value(row, "sender_type", "") or "").strip() in ("AI", "System", "Bot"):
            continue
        if _inbound_triggers_autopilot(row):
            return True
    return False


def _inbound_triggers_autopilot(doc) -> bool:
    content_type = str(_doc_value(doc, "content_type", None) or "Text").title()
    if _is_media_message(doc):
        return True
    return bool(_meaningful_body(str(_doc_value(doc, "body", None) or ""), content_type))


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
    rows = safe_ai_get_all(
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
        return _truncate_history_text(body)
    if str(row.media_url or "").strip() and content_type in MEDIA_CONTENT_TYPES:
        return f"[sent {content_type}]"
    return ""


def _truncate_history_text(text: str, limit: int = MAX_HISTORY_MESSAGE_CHARS) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    return (
        text[:head].rstrip()
        + "\n[Earlier part shortened to keep full chat context within model limits.]\n"
        + text[-tail:].lstrip()
    )


def _safe_log_error(title: str, message: str) -> None:
    try:
        frappe.log_error(
            title=str(title or "")[:140],
            message=_sanitize_log_message(message)[:4000],
        )
    except Exception:
        pass


def _sanitize_log_message(message: str) -> str:
    text = str(message or "")
    redactions = (
        (r"sk-[A-Za-z0-9_\-]{12,}", "sk-[redacted]"),
        (r"sk-proj-[A-Za-z0-9_\-]{12,}", "sk-proj-[redacted]"),
        (r"(?i)(api[_-]?key|authorization|token|password|secret)\s*[:=]\s*['\"]?[^'\"\s,}]+", r"\1=[redacted]"),
    )
    for pattern, replacement in redactions:
        text = re.sub(pattern, replacement, text)
    return text


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


def _looks_like_degenerate_reply(text: str) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return True

    words = re.findall(r"[\wऀ-ॿ]+", cleaned.lower())
    if len(words) < 8:
        return False

    token_counts = {}
    for word in words:
        token_counts[word] = token_counts.get(word, 0) + 1
    most_common_count = max(token_counts.values()) if token_counts else 0
    if most_common_count >= 12 and most_common_count / max(len(words), 1) >= 0.35:
        return True

    for size in (2, 3):
        if len(words) < size * 6:
            continue
        phrases = [" ".join(words[i : i + size]) for i in range(len(words) - size + 1)]
        phrase_counts = {}
        for phrase in phrases:
            phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1
        if max(phrase_counts.values(), default=0) >= 6:
            return True

    return False


def _conversation_allows_autopilot(conversation: str) -> bool:
    status = safe_ai_get_value("Chat Conversation", conversation, "status")
    return status in (None, "", "Open")


def _already_replied_to_inbound(conversation: str, inbound_message_id: str) -> bool:
    """Only skip duplicate work for the same inbound message, not the whole conversation."""
    inbound_creation = safe_ai_get_value("Chat Message", inbound_message_id, "creation")
    if not inbound_creation:
        return False

    # Raw SQL is used for this duplicate-reply lookup, so gate it explicitly.
    assert_ai_doctype_permission("Chat Message", "read")
    prior_ai = frappe.db.sql(
        """
        SELECT delivery_status, raw_transport_payload
        FROM `tabChat Message`
        WHERE conversation = %s
          AND direction = 'Outbound'
          AND sender_type = 'AI'
          AND creation > %s
        ORDER BY creation ASC
        LIMIT 10
        """,
        (conversation, inbound_creation),
        as_dict=True,
    )
    for row in prior_ai:
        try:
            payload = frappe.parse_json(row.raw_transport_payload) if row.raw_transport_payload else {}
        except Exception:
            payload = {}
        if str((payload or {}).get("reply_to_message") or "") != str(inbound_message_id):
            continue
        if (row.delivery_status or "") in ("Failed", "Pending"):
            return False
        return True
    return False


def _load_providers(preferred_provider: str | None = None):
    rows = get_active_llm_provider_rows(CHAT_CAPABILITY)
    if preferred_provider:
        rows = sorted(rows, key=lambda row: 0 if row.name == preferred_provider else 1)
    providers = []
    for row in rows:
        doc = safe_ai_get_doc("WA LLM Provider", row.name)
        try:
            api_key = doc.get_password("api_key", raise_exception=False)
        except Exception as exc:
            _log_ai_timing(
                "provider_secret_failed",
                provider=row.name,
                provider_type=row.provider_type,
                model=row.model_name,
                error=str(exc)[:140],
            )
            _safe_log_error(
                "WA AI Provider Config Error",
                (
                    f"Provider {row.name} ({row.provider_type}, model {row.model_name}) "
                    f"has an unreadable API key. Re-save the provider secret. Error: {str(exc)[:300]}"
                ),
            )
            continue
        if not api_key:
            _log_ai_timing(
                "provider_secret_missing",
                provider=row.name,
                provider_type=row.provider_type,
                model=row.model_name,
            )
            _safe_log_error(
                "WA AI Provider Config Error",
                (
                    f"Provider {row.name} ({row.provider_type}, model {row.model_name}) "
                    "has no readable API key. Re-save the provider secret."
                ),
            )
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
    convo = safe_ai_get_doc("Chat Conversation", conversation)
    phone_number = safe_ai_get_value("Chat Contact", convo.contact, "phone_number")
    reply_to_message = (
        getattr(frappe.local, "wa_ai_reply_to_message", None)
        or getattr(frappe.flags, "wa_ai_reply_to_message", None)
    )

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
                "raw_transport_payload": {
                    **outbound,
                    "source": "ai_autopilot",
                    "reply_to_message": reply_to_message,
                },
            }
        )
    except Exception as exc:
        _log_ai_timing(
            "send_append_failed",
            conversation=conversation,
            delivery_status=delivery_status,
            provider_message_id=channel_message_id,
            error=str(exc)[:140],
        )
        _safe_log_error(
            "WA AI Autopilot Append Failed",
            (
                f"AI reply send completed with delivery_status={delivery_status}, "
                f"provider_message_id={channel_message_id or '-'}, but local Chat Message append failed "
                f"for conversation {conversation}: {str(exc)[:500]}"
            ),
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


def call_provider(
    provider,
    system_prompt,
    history,
    latest_user_text=None,
    current_inbound=None,
    allowed_tool_names=None,
    max_tool_calls=0,
    tool_context=None,
):
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

    is_buopso_vllm = "vllm.buopso.net" in str(provider.base_url or "").lower()
    if provider.provider_type in ("OpenAI", "Custom"):
        return call_openai_format(
            provider,
            messages,
            timeout=15 if is_buopso_vllm else 45,
            allowed_tool_names=allowed_tool_names,
            max_tool_calls=max_tool_calls,
            tool_context=tool_context,
        )
    if provider.provider_type == "Gemini":
        if not provider.base_url:
            provider.base_url = (
                "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            )
        return call_openai_format(
            provider,
            messages,
            timeout=45,
            allowed_tool_names=allowed_tool_names,
            max_tool_calls=max_tool_calls,
            tool_context=tool_context,
        )
    if provider.provider_type == "Anthropic":
        raise Exception(
            "Anthropic specific MCP format requires SDK. Please use OpenAI/Gemini/Custom."
        )

    raise Exception(f"Unsupported provider type {provider.provider_type}")


def fetch_mcp_tools(allowed_tool_names=None):
    assert_ai_doctype_permission("WA Chat Hub Settings", "read")
    settings = frappe.get_single("WA Chat Hub Settings")
    if not getattr(settings, "allow_mcp_access", 0):
        return []
    if not safe_ai_exists("DocType", "WA MCP Tool Endpoint"):
        return []

    allowed_tool_names = {str(name).strip() for name in (allowed_tool_names or []) if name}
    if not allowed_tool_names:
        return []
    tools_docs = safe_ai_get_all(
        "WA MCP Tool Endpoint",
        filters={"is_active": 1, "tool_name": ["in", sorted(allowed_tool_names)]},
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


def execute_mcp_tool(tool_name, arguments_dict, allowed_tool_names=None, tool_context=None):
    tools = fetch_mcp_tools(allowed_tool_names)
    tool_meta = next((t["_meta"] for t in tools if t["function"]["name"] == tool_name), None)
    if not tool_meta:
        return f"Error: Tool {tool_name} not found."

    try:
        url = tool_meta["url"]
        arguments_dict = dict(arguments_dict or {})
        tool_context = tool_context or {}
        if tool_context.get("patient"):
            arguments_dict["patient"] = tool_context["patient"]
        arguments_dict.pop("patient_id", None)

        if url.startswith("http"):
            if tool_meta["method"] == "POST":
                resp = requests.post(url, json=arguments_dict, timeout=10)
            else:
                resp = requests.get(url, params=arguments_dict, timeout=10)
            resp.raise_for_status()
            if "application/json" in (resp.headers.get("Content-Type") or ""):
                return json.dumps(resp.json())
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


def _strip_model_reasoning(text: str) -> str:
    text = str(text or "")
    if not text:
        return ""
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    return text.strip()


def _is_low_context_provider(provider) -> bool:
    base_url = str(getattr(provider, "base_url", "") or "").lower()
    model = str(getattr(provider, "model_name", "") or "").lower()
    return "openrouter.ai" in base_url or "vllm.buopso.net" in base_url or "glm" in model


def _truncate_message_content(content, limit: int):
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        remaining = limit
        trimmed = []
        for part in content:
            if not isinstance(part, dict):
                continue
            item = dict(part)
            text = item.get("text")
            if isinstance(text, str):
                item["text"] = text[:remaining]
                remaining -= len(item["text"])
            trimmed.append(item)
            if remaining <= 0:
                break
        return trimmed
    return content


def _fit_messages_for_provider(provider, messages: list[dict]) -> list[dict]:
    if not _is_low_context_provider(provider):
        return messages

    if not messages:
        return messages

    system_message = dict(messages[0])
    system_content = str(system_message.get("content") or "")
    if len(system_content) > LOW_CONTEXT_SYSTEM_CHAR_BUDGET:
        head_budget = int(LOW_CONTEXT_SYSTEM_CHAR_BUDGET * 0.65)
        tail_budget = LOW_CONTEXT_SYSTEM_CHAR_BUDGET - head_budget
        system_message["content"] = (
            system_content[:head_budget]
            + "\n\n[Prompt middle shortened for provider context limit. Continue following role, safety, healthcare-segment, stop-rule, and appointment/callback rules. Important recent context continues below.]\n\n"
            + system_content[-tail_budget:]
        )

    kept = [system_message]
    remaining_budget = LOW_CONTEXT_INPUT_CHAR_BUDGET - len(str(system_message.get("content") or ""))
    for message in reversed(messages[1:]):
        content = message.get("content")
        content_len = len(str(content or ""))
        if remaining_budget <= 0:
            break
        if content_len > min(1200, remaining_budget):
            message = dict(message)
            message["content"] = _truncate_message_content(content, min(1200, remaining_budget))
            content_len = len(str(message.get("content") or ""))
        kept.insert(1, message)
        remaining_budget -= content_len
    return kept


def call_openai_format(
    provider,
    messages,
    timeout=20,
    allowed_tool_names=None,
    max_tool_calls=0,
    tool_context=None,
):
    request_started = time.monotonic()
    url = provider.base_url or "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }

    if url.endswith("/") and "chat/completions" not in url:
        url += "chat/completions"
    messages = _fit_messages_for_provider(provider, messages)

    tools = fetch_mcp_tools(allowed_tool_names)
    api_tools = [{"type": t["type"], "function": t["function"]} for t in tools] if tools else None
    token_limit_key = _max_tokens_payload_key(provider.model_name)
    is_vllm_provider = "vllm.buopso.net" in str(url).lower()
    request_timeout = timeout

    output_token_limit = 900 if is_vllm_provider else 500
    payload = {
        "model": provider.model_name,
        "messages": messages,
        token_limit_key: output_token_limit,
    }
    if is_vllm_provider:
        payload["temperature"] = 0.2
    else:
        payload.update(
            {
                "temperature": 0.75,
                "presence_penalty": 0.4,
                "frequency_penalty": 0.3,
            }
        )
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
        timeout_sec=request_timeout,
    )
    resp = requests.post(url, headers=headers, json=payload, timeout=request_timeout)
    _log_ai_timing(
        "api_request_done",
        provider=provider.name,
        model=provider.model_name,
        status_code=resp.status_code,
        duration_sec=elapsed(request_started),
    )

    if resp.status_code != 200:
        _safe_log_error(
            "WA AI Provider API Failure",
            f"API Error {resp.status_code}: {resp.text}",
        )
        resp.raise_for_status()

    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        _safe_log_error(
            "WA AI Provider API Failure",
            f"OpenAI empty choices for model {provider.model_name}: {resp.text[:500]}",
        )
        return ""

    message = choices[0].get("message") or {}

    if message.get("tool_calls"):
        messages.append(message)

        tool_calls = message["tool_calls"][: max(0, int(max_tool_calls or 0))]
        if not tool_calls:
            return "I’m unable to access the required record safely right now. I’ll connect you with our care team."
        for tc in tool_calls:
            tool_started = time.monotonic()
            try:
                args = json.loads(tc["function"]["arguments"])
            except Exception:
                args = {}
            tool_res = execute_mcp_tool(
                tc["function"]["name"],
                args,
                allowed_tool_names=allowed_tool_names,
                tool_context=tool_context,
            )
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
        resp = requests.post(url, headers=headers, json=payload, timeout=request_timeout)
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
        return _strip_model_reasoning((follow_choices[0].get("message") or {}).get("content", "") or "")

    return _strip_model_reasoning(message.get("content", "") or "")
