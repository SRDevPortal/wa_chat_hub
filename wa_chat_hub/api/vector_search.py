"""Lightweight knowledge retrieval for AI autopilot (keyword match on WA AI Knowledge Base)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import frappe

from wa_chat_hub.settings import get_active_knowledge_base


def search_knowledge_base(
    query: str,
    top_k: int = 3,
    department: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Return relevant knowledge snippets for the user query.
    Uses active WA AI Knowledge Base rows (no embedding dependency).
    """
    query = (query or "").strip()
    if not query or not frappe.db.exists("DocType", "WA AI Knowledge Base"):
        return []

    rows = get_active_knowledge_base(department=department)
    if not rows:
        return []

    tokens = _tokenize(query)
    if not tokens:
        return []

    scored: List[tuple[int, Dict[str, Any]]] = []
    for row in rows:
        content = (row.get("content") or "").strip()
        if not content:
            continue
        title = row.get("kb_label") or row.get("name") or "Knowledge"
        haystack = f"{title}\n{content}".lower()
        score = sum(1 for token in tokens if token in haystack)
        if score:
            scored.append(
                (
                    score,
                    {
                        "title": title,
                        "content": content[:4000],
                        "kb_type": row.get("kb_type"),
                        "name": row.get("name"),
                    },
                )
            )

    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[: max(1, int(top_k or 3))]]


def _tokenize(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]{3,}", text.lower())
    seen = set()
    out = []
    for word in words:
        if word not in seen:
            seen.add(word)
            out.append(word)
    return out[:20]
