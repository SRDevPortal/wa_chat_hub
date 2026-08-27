from __future__ import annotations

from typing import Dict, List

from wa_chat_hub.settings import get_active_knowledge_base
from wa_chat_hub.security import safe_ai_get_all, safe_ai_insert

import frappe

PROMPT_SYSTEM = """You are a healthcare-safe WhatsApp copilot for ERPNext.
Never provide diagnosis or medication instructions unless explicitly allowed by approved workflow.
Prefer summarization, intent extraction, routing, structured drafting, and safe reply suggestions.
"""


def build_conversation_context(conversation: str, limit: int = 30) -> List[Dict[str, str]]:
    rows = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["direction", "sender_type", "body", "creation"],
        order_by="creation desc",
        limit_page_length=limit,
    )
    return list(reversed(rows))


def create_ai_suggestion(conversation: str, suggestion_type: str, content: str) -> str:
    doc = frappe.get_doc({
        "doctype": "Chat AI Suggestion",
        "conversation": conversation,
        "suggestion_type": suggestion_type,
        "content": content,
        "status": "Draft",
    })
    safe_ai_insert(doc)
    return doc.name


def heuristic_intent_and_department(messages: List[Dict[str, str]]) -> Dict[str, str]:
    text = "\n".join([(m.get("body") or "") for m in messages]).lower()
    if any(word in text for word in ["appointment", "book", "consult"]):
        return {"intent": "Appointment", "department_hint": "General"}
    if any(word in text for word in ["order", "delivery", "parcel", "shipped"]):
        return {"intent": "Order Support", "department_hint": "Support"}
    if any(word in text for word in ["skin", "acne", "eczema"]):
        return {"intent": "Medical Inquiry", "department_hint": "Skin"}
    return {"intent": "General Inquiry", "department_hint": "General"}


def generate_summary(conversation: str) -> Dict[str, str]:
    messages = build_conversation_context(conversation)
    joined = " ".join([(m.get("body") or "") for m in messages]).strip()
    intent = heuristic_intent_and_department(messages)
    summary = (joined[:600] + "...") if len(joined) > 600 else joined
    suggestion = f"Intent: {intent['intent']}\nDepartment Hint: {intent['department_hint']}\nSummary: {summary or 'No text available.'}"
    name = create_ai_suggestion(conversation, "Summary", suggestion)
    return {"name": name, "content": suggestion}


def generate_reply_draft(conversation: str) -> Dict[str, str]:
    messages = build_conversation_context(conversation)
    intent = heuristic_intent_and_department(messages)
    content = (
        f"Draft reply ({intent['intent']}): "
        "Thank you for reaching out. We’ve noted your request and a relevant team member will assist you shortly. "
        "If needed, please share your name, concern, and any previous patient ID."
    )
    name = create_ai_suggestion(conversation, "Reply Draft", content)
    return {"name": name, "content": content}


def get_knowledge_snippets(department: str | None = None) -> List[Dict[str, str]]:
    rows = get_active_knowledge_base(department=department)
    return rows[:10]


def build_ai_runtime_bundle(conversation: str, department: str | None = None) -> Dict[str, object]:
    return {
        "messages": build_conversation_context(conversation),
        "knowledge_base": get_knowledge_snippets(department=department),
    }
