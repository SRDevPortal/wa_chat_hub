from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now_datetime

from wa_chat_hub.ai.configuration import (
    IntentRouteDefinition,
    WorkflowDefinition,
    load_workflow,
    normalize_match_text,
)
from wa_chat_hub.security import safe_ai_get_all, safe_ai_get_value, safe_ai_set_value


STATE_VERSION = 1
RESERVED_EXECUTION_ARGUMENTS = {"conversation", "patient", "message", "channel_account"}
INTERACTION_TOKEN_KEYS = {
    "callbackdata",
    "callback_data",
    "buttonpayload",
    "button_payload",
    "payload",
    "workflow_confirmation_token",
}


@dataclass(frozen=True)
class WorkflowPolicy:
    handled: bool = False
    reply: str | None = None
    execute: bool = False
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] | None = None
    outbound_template: dict[str, Any] | None = None


def load_active_workflow_state(conversation: str | None) -> dict[str, Any] | None:
    if not conversation:
        return None
    row = safe_ai_get_value(
        "Chat Conversation",
        conversation,
        ["active_ai_workflow", "ai_workflow_state", "workflow_expires_at"],
        as_dict=True,
    )
    if not row or not row.get("active_ai_workflow") or not row.get("ai_workflow_state"):
        return None
    try:
        state = frappe.parse_json(row.get("ai_workflow_state"))
    except Exception:
        clear_active_workflow(conversation)
        return None
    if not isinstance(state, dict) or state.get("workflow") != row.get("active_ai_workflow"):
        clear_active_workflow(conversation)
        return None
    expires_at = row.get("workflow_expires_at") or state.get("expires_at")
    try:
        if expires_at and get_datetime(expires_at) <= now_datetime():
            clear_active_workflow(conversation)
            state["expired"] = True
            return state
    except Exception:
        clear_active_workflow(conversation)
        return None
    return state


def evaluate_workflow_policy(
    *,
    conversation: str,
    message_id: str,
    body_text: str | None,
    intent_decision: Any,
    intent_route: IntentRouteDefinition | None,
    raw_payload: Any = None,
) -> WorkflowPolicy:
    body = str(body_text or "").strip()
    state = load_active_workflow_state(conversation)
    if state and state.get("expired"):
        workflow = load_workflow(state.get("workflow"))
        reply = _message(workflow, "expired") if workflow else ""
        return WorkflowPolicy(handled=bool(reply), reply=reply or None)

    if state:
        workflow = load_workflow(state.get("workflow"))
        if not workflow or int(state.get("workflow_version") or 0) != workflow.version:
            clear_active_workflow(conversation)
            return WorkflowPolicy()
        return _advance_workflow(
            conversation=conversation,
            message_id=message_id,
            body=body,
            raw_payload=raw_payload,
            workflow=workflow,
            state=state,
        )

    workflow = load_workflow(getattr(intent_route, "ai_workflow", None))
    if not workflow:
        return WorkflowPolicy()
    trigger = workflow.definition.get("trigger") or {}
    minimum = max(0.0, min(1.0, float(trigger.get("minimum_confidence") or 0.0)))
    if float(getattr(intent_decision, "confidence", 0.0) or 0.0) < minimum:
        return WorkflowPolicy()

    state = {
        "state_version": STATE_VERSION,
        "instance_id": secrets.token_urlsafe(18),
        "workflow": workflow.name,
        "workflow_version": workflow.version,
        "action_tool": workflow.action_tool,
        "stage": "collecting",
        "field_index": 0,
        "collected": {},
        "origin_message": body[:2000],
        "origin_message_id": str(message_id),
    }
    collect = workflow.definition.get("collect") or []
    state["collected"] = _prefill_collect_fields(conversation, collect)
    next_index = _next_missing_collect_index(collect, state["collected"], 0)
    state["field_index"] = next_index
    if next_index < len(collect):
        _save_state(conversation, message_id, state, workflow)
        return WorkflowPolicy(
            handled=True,
            reply=_render(str(collect[next_index].get("prompt") or ""), state),
            state=state,
        )
    return _begin_confirmation(conversation, message_id, workflow, state)


def workflow_missing_fields(state: dict[str, Any] | None) -> list[str]:
    if not state:
        return []
    workflow = load_workflow(state.get("workflow"))
    if not workflow:
        return []
    collected = state.get("collected") or {}
    missing = [
        str(step.get("field"))
        for step in workflow.definition.get("collect") or []
        if step.get("field") and step.get("field") not in collected
    ]
    if state.get("stage") in {"collecting", "awaiting_confirmation"}:
        missing.append("customer_confirmation")
    return missing


def validate_workflow_execution(
    conversation: str | None,
    tool_name: str | None,
    supplied_arguments: dict[str, Any] | None,
) -> dict[str, Any]:
    conversation_name = str(conversation or "").strip()
    action_tool = str(tool_name or "").strip()
    state = load_active_workflow_state(conversation_name)
    if not state or state.get("expired") or state.get("stage") != "authorized":
        frappe.throw(_("A current server-authorized workflow confirmation is required."))
    workflow = load_workflow(state.get("workflow"))
    if not workflow or workflow.action_tool != action_tool or state.get("action_tool") != action_tool:
        frappe.throw(_("The pending workflow does not authorize this MCP tool."))
    if int(state.get("workflow_version") or 0) != workflow.version:
        frappe.throw(_("The pending workflow configuration has changed and must be restarted."))
    if not _confirmation_prompt_was_delivered(conversation_name, state):
        frappe.throw(_("The workflow confirmation prompt was not delivered to the customer."))

    latest = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation_name, "direction": "Inbound"},
        fields=["name", "body", "raw_payload", "raw_transport_payload"],
        order_by="creation desc, name desc",
        limit=1,
    )
    latest_message = latest[0] if latest else None
    if not latest_message or str(latest_message.get("name")) != str(state.get("confirmation_message") or ""):
        frappe.throw(_("The latest customer message is not the workflow confirmation."))
    candidates = _confirmation_candidates(
        latest_message.get("body"),
        {
            "raw_payload": latest_message.get("raw_payload"),
            "raw_transport_payload": latest_message.get("raw_transport_payload"),
        },
    )
    if not _candidate_matches(state, candidates):
        frappe.throw(_("The latest customer message does not contain the valid one-time confirmation."))

    expected = build_workflow_arguments(workflow, state, str(latest_message.get("body") or ""))
    supplied = dict(supplied_arguments or {})
    for key, value in expected.items():
        if supplied.get(key) != value:
            frappe.throw(_("Workflow argument {0} does not match server-authorized state.").format(key))
    unexpected = set(supplied) - set(expected) - RESERVED_EXECUTION_ARGUMENTS
    if unexpected:
        frappe.throw(_("Workflow received unauthorized arguments: {0}").format(", ".join(sorted(unexpected))))
    return state


def complete_workflow_execution(conversation: str | None, tool_name: str | None) -> None:
    state = load_active_workflow_state(conversation)
    if state and state.get("action_tool") == str(tool_name or "").strip():
        clear_active_workflow(conversation)


def clear_active_workflow(conversation: str | None) -> None:
    if not conversation:
        return
    safe_ai_set_value(
        "Chat Conversation",
        str(conversation),
        {
            "active_ai_workflow": None,
            "ai_workflow_state": None,
            "workflow_expires_at": None,
            "workflow_last_message": None,
        },
        update_modified=False,
    )


def build_workflow_arguments(
    workflow: WorkflowDefinition,
    state: dict[str, Any],
    latest_message: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for fieldname, spec in (workflow.definition.get("arguments") or {}).items():
        source = str(spec.get("source") or "")
        if source == "value":
            value = spec.get("value")
        elif source == "origin_message":
            value = state.get("origin_message")
        elif source == "latest_message":
            value = latest_message
        elif source == "collected":
            value = (state.get("collected") or {}).get(str(spec.get("field") or ""))
        else:
            continue
        if value in (None, "") and "default" in spec:
            value = spec.get("default")
        result[str(fieldname)] = value
    return result


def _advance_workflow(
    *,
    conversation: str,
    message_id: str,
    body: str,
    raw_payload: Any,
    workflow: WorkflowDefinition,
    state: dict[str, Any],
) -> WorkflowPolicy:
    cancel_phrases = {
        normalize_match_text(item)
        for item in (workflow.definition.get("confirmation") or {}).get("cancel_phrases") or []
        if str(item or "").strip()
    }
    if normalize_match_text(body) in cancel_phrases:
        clear_active_workflow(conversation)
        return WorkflowPolicy(handled=True, reply=_message(workflow, "cancelled") or None)

    stage = str(state.get("stage") or "collecting")
    if stage == "collecting":
        collect = workflow.definition.get("collect") or []
        index = max(0, int(state.get("field_index") or 0))
        if index >= len(collect):
            return _begin_confirmation(conversation, message_id, workflow, state)
        step = collect[index]
        if not _valid_collected_value(body, step.get("validation") or {}):
            return WorkflowPolicy(
                handled=True,
                reply=_render(str(step.get("invalid_prompt") or step.get("prompt") or ""), state),
                state=state,
            )
        updated = dict(state)
        collected = dict(state.get("collected") or {})
        collected[str(step.get("field"))] = body[:4000]
        updated["collected"] = collected
        next_index = _next_missing_collect_index(collect, collected, index + 1)
        updated["field_index"] = next_index
        if next_index < len(collect):
            _save_state(conversation, message_id, updated, workflow)
            return WorkflowPolicy(
                handled=True,
                reply=_render(str(collect[next_index].get("prompt") or ""), updated),
                state=updated,
            )
        return _begin_confirmation(conversation, message_id, workflow, updated)

    if stage == "awaiting_confirmation":
        candidates = _confirmation_candidates(body, raw_payload)
        if not _candidate_matches(state, candidates):
            confirmation = workflow.definition.get("confirmation") or {}
            return WorkflowPolicy(
                handled=True,
                reply=_render(
                    str(confirmation.get("invalid_prompt") or state.get("confirmation_prompt") or ""),
                    state,
                ),
                state=state,
            )
        if not _confirmation_prompt_was_delivered(conversation, state):
            return WorkflowPolicy(
                handled=True,
                reply=str(state.get("confirmation_prompt") or "").strip() or None,
                state=state,
            )
        updated = dict(state)
        updated["stage"] = "authorized"
        updated["confirmation_message"] = str(message_id)
        _save_state(conversation, message_id, updated, workflow)
        return WorkflowPolicy(
            handled=True,
            execute=True,
            tool_name=workflow.action_tool,
            arguments=build_workflow_arguments(workflow, updated, body),
            state=updated,
        )

    if stage == "authorized" and str(state.get("confirmation_message") or "") == str(message_id):
        candidates = _confirmation_candidates(body, raw_payload)
        if _candidate_matches(state, candidates):
            return WorkflowPolicy(
                handled=True,
                execute=True,
                tool_name=workflow.action_tool,
                arguments=build_workflow_arguments(workflow, state, body),
                state=state,
            )
    return WorkflowPolicy(handled=True, state=state)


def _begin_confirmation(
    conversation: str,
    message_id: str,
    workflow: WorkflowDefinition,
    state: dict[str, Any],
) -> WorkflowPolicy:
    confirmation = workflow.definition.get("confirmation") or {}
    code_length = max(4, min(12, int(confirmation.get("challenge_length") or 6)))
    code = "".join(secrets.choice("0123456789") for _ in range(code_length))
    interaction_token = secrets.token_urlsafe(24)
    updated = dict(state)
    updated["stage"] = "awaiting_confirmation"
    updated["confirmation_after_message"] = str(message_id)
    updated["confirmation_digest"] = _challenge_digest(updated, code)
    updated["interaction_digest"] = _challenge_digest(updated, interaction_token)
    render_state = dict(updated)
    render_state["confirmation_code"] = code
    render_state["interaction_token"] = interaction_token
    prompt = _render(str(confirmation.get("prompt") or ""), render_state)
    updated["confirmation_prompt"] = prompt
    outbound_template = None
    if str(confirmation.get("mode") or "") == "interakt_template":
        template = confirmation.get("template") or {}
        if isinstance(template, dict):
            outbound_template = _render_nested(template, render_state)
    _save_state(conversation, message_id, updated, workflow)
    return WorkflowPolicy(
        handled=True,
        reply=prompt or None,
        state=updated,
        outbound_template=outbound_template,
    )


def _save_state(
    conversation: str,
    message_id: str,
    state: dict[str, Any],
    workflow: WorkflowDefinition,
) -> None:
    now = now_datetime()
    expires_at = add_to_date(now, minutes=workflow.expiry_minutes, as_string=True)
    value = dict(state)
    value["updated_at"] = str(now)
    value["expires_at"] = expires_at
    safe_ai_set_value(
        "Chat Conversation",
        conversation,
        {
            "active_ai_workflow": workflow.name,
            "ai_workflow_state": json.dumps(value, ensure_ascii=False),
            "workflow_expires_at": expires_at,
            "workflow_last_message": str(message_id),
        },
        update_modified=False,
    )


def _valid_collected_value(value: str, validation: dict[str, Any]) -> bool:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if len(text) < int(validation.get("min_length") or 0):
        return False
    maximum = int(validation.get("max_length") or 0)
    if maximum and len(text) > maximum:
        return False
    if len(text.split()) < int(validation.get("min_words") or 0):
        return False
    if validation.get("excludes_digit") and any(char.isdigit() for char in text):
        return False
    if validation.get("contains_alpha") and not any(char.isalpha() for char in text):
        return False
    configured_pattern = str(validation.get("pattern") or "").strip()
    if configured_pattern:
        try:
            if not re.fullmatch(configured_pattern, text):
                return False
        except re.error:
            return False
    if validation.get("contains_digit") and not any(char.isdigit() for char in text):
        return False
    normalized = normalize_match_text(text)
    contains_any = [normalize_match_text(item) for item in validation.get("contains_any") or []]
    if contains_any and not any(item and item in normalized for item in contains_any):
        return False
    contains_all = [normalize_match_text(item) for item in validation.get("contains_all") or []]
    if contains_all and not all(item and item in normalized for item in contains_all):
        return False
    allowed_lengths = {int(item) for item in validation.get("digit_group_lengths_any") or []}
    if allowed_lengths and not any(len(group) in allowed_lengths for group in re.findall(r"\d+", text)):
        return False
    return True


def _next_missing_collect_index(
    collect: list[dict[str, Any]],
    collected: dict[str, Any],
    start: int,
) -> int:
    index = max(0, int(start or 0))
    while index < len(collect):
        fieldname = str(collect[index].get("field") or "").strip()
        if fieldname and collected.get(fieldname) in (None, ""):
            return index
        index += 1
    return len(collect)


def _prefill_collect_fields(
    conversation: str,
    collect: list[dict[str, Any]],
) -> dict[str, Any]:
    prefillable = [
        step
        for step in collect
        if isinstance(step, dict) and isinstance(step.get("prefill"), dict)
    ]
    if not prefillable:
        return {}

    sources = [
        source
        for step in prefillable
        for source in _prefill_sources(step.get("prefill") or {})
    ]
    maximum = max(
        [int(source.get("max_messages") or 0) for source in sources]
        or [0]
    )
    maximum = max(1, min(100, maximum or 30))
    rows = safe_ai_get_all(
        "Chat Message",
        filters={"conversation": conversation, "direction": "Inbound"},
        fields=["body"],
        order_by="creation desc, name desc",
        limit=maximum,
    )
    messages = [str(row.get("body") or "").strip() for row in rows if row.get("body")]
    context = _prefill_context(conversation)
    result: dict[str, Any] = {}
    for step in prefillable:
        fieldname = str(step.get("field") or "").strip()
        validation = step.get("validation") or {}
        for value in _prefill_candidates(
            step.get("prefill") or {}, messages, context
        ):
            if fieldname and value not in (None, "") and _valid_collected_value(
                value, validation
            ):
                result[fieldname] = value
                break
    return result


def _prefill_sources(prefill: dict[str, Any]) -> list[dict[str, Any]]:
    configured = prefill.get("sources")
    if isinstance(configured, list):
        return [source for source in configured if isinstance(source, dict)]
    return [prefill] if prefill else []


def _prefill_context(conversation: str) -> dict[str, Any]:
    row = safe_ai_get_value(
        "Chat Conversation",
        conversation,
        ["contact", "linked_crm_lead", "linked_reference_doctype", "linked_reference_name"],
        as_dict=True,
    )
    context = dict(row or {})
    contact = str(context.get("contact") or "").strip()
    context["phone_number"] = (
        safe_ai_get_value("Chat Contact", contact, "phone_number") if contact else ""
    )
    return context


def _prefill_candidates(
    prefill: dict[str, Any],
    messages: list[str],
    context: dict[str, Any] | None = None,
) -> list[str]:
    candidates: list[str] = []
    for source in _prefill_sources(prefill):
        source_name = str(source.get("source") or "").strip()
        if source_name == "recent_message_regex":
            value = _regex_prefill_value(source, messages)
            if value:
                candidates.append(value)
        elif source_name == "linked_record_fields":
            candidates.extend(_linked_record_prefill_values(source, context or {}))
        elif source_name == "phone_match_fields":
            candidates.extend(_phone_match_prefill_values(source, context or {}))
    return candidates


def _prefill_value(
    prefill: dict[str, Any],
    messages: list[str],
    context: dict[str, Any] | None = None,
) -> str:
    candidates = _prefill_candidates(prefill, messages, context)
    return candidates[0] if candidates else ""


def _regex_prefill_value(prefill: dict[str, Any], messages: list[str]) -> str:
    patterns = [
        str(pattern).strip()
        for pattern in prefill.get("patterns") or []
        if str(pattern or "").strip()
    ]
    if not patterns:
        return ""
    try:
        group = max(0, int(prefill.get("group") or 1))
    except (TypeError, ValueError):
        group = 1
    excluded = {
        normalize_match_text(value)
        for value in prefill.get("exclude_values") or []
        if str(value or "").strip()
    }
    for message in messages:
        for pattern in patterns:
            try:
                match = re.search(pattern, message, flags=re.UNICODE)
                value = str(match.group(group) if match else "").strip(" \t\r\n.,;:!?-'\"")
            except (IndexError, re.error):
                continue
            if value and normalize_match_text(value) not in excluded:
                return value[:4000]
    return ""


def _linked_record_prefill_values(
    source: dict[str, Any], context: dict[str, Any]
) -> list[str]:
    doctype = str(source.get("doctype") or "").strip()
    link_field = str(source.get("conversation_link_field") or "").strip()
    record_name = str(context.get(link_field) or "").strip()
    value_fields = _configured_fieldnames(source.get("value_fields"))
    if not doctype or not record_name or not value_fields:
        return []
    row = safe_ai_get_value(doctype, record_name, value_fields, as_dict=True)
    return _row_prefill_values(
        row,
        value_fields,
        combine=bool(source.get("combine_value_fields")),
    )


def _phone_match_prefill_values(
    source: dict[str, Any], context: dict[str, Any]
) -> list[str]:
    doctype = str(source.get("doctype") or "").strip()
    phone_fields = _configured_fieldnames(source.get("phone_fields"))
    value_fields = _configured_fieldnames(source.get("value_fields"))
    phone_values = _exact_phone_values(str(context.get("phone_number") or ""))
    if not doctype or not phone_fields or not value_fields or not phone_values:
        return []
    limit = max(1, min(int(source.get("max_records") or 5), 20))
    rows: list[Any] = []
    seen: set[str] = set()
    for phone_field in phone_fields:
        for phone_value in phone_values:
            matches = safe_ai_get_all(
                doctype,
                filters={phone_field: phone_value},
                fields=["name", *value_fields],
                limit_page_length=limit,
            )
            for row in matches:
                name = str(row.get("name") or "")
                if name and name not in seen:
                    rows.append(row)
                    seen.add(name)
                if len(rows) >= limit:
                    break
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    return [
        value
        for row in rows
        for value in _row_prefill_values(
            row,
            value_fields,
            combine=bool(source.get("combine_value_fields")),
        )
    ]


def _configured_fieldnames(value: Any) -> list[str]:
    return [
        str(fieldname).strip()
        for fieldname in (value or [])
        if str(fieldname or "").strip()
    ][:20]


def _row_prefill_values(
    row: Any, value_fields: list[str], *, combine: bool = False
) -> list[str]:
    if not row:
        return []
    values = [
        str(row.get(fieldname) or "").strip()
        for fieldname in value_fields
        if str(row.get(fieldname) or "").strip()
    ]
    return [" ".join(values)] if combine and values else values


def _exact_phone_values(phone_number: str) -> list[str]:
    raw = str(phone_number or "").strip()
    digits = "".join(char for char in raw if char.isdigit())
    values = [raw, digits]
    if len(digits) >= 10:
        values.append(digits[-10:])
    if digits:
        values.append(f"+{digits}")
    return list(dict.fromkeys(value for value in values if value))


def _confirmation_prompt_was_delivered(conversation: str, state: dict[str, Any]) -> bool:
    after_message = str(state.get("confirmation_after_message") or "").strip()
    expected = str(state.get("confirmation_prompt") or "").strip()
    if not after_message or not expected:
        return False
    creation = safe_ai_get_value("Chat Message", after_message, "creation")
    if not creation:
        return False
    rows = safe_ai_get_all(
        "Chat Message",
        filters={
            "conversation": conversation,
            "direction": "Outbound",
            "creation": [">", creation],
            "delivery_status": ["not in", ["Failed", "Pending"]],
        },
        fields=["body"],
        order_by="creation asc, name asc",
        limit=30,
    )
    return any(str(row.get("body") or "").strip() == expected for row in rows)


def _confirmation_candidates(body: str | None, raw_payload: Any) -> set[str]:
    candidates = {str(body or "").strip()}
    _collect_interaction_candidates(_coerce_json(raw_payload), candidates)
    return {value for value in candidates if value}


def _collect_interaction_candidates(value: Any, result: set[str], key: str = "") -> None:
    normalized_key = key.casefold()
    token_container = normalized_key in INTERACTION_TOKEN_KEYS
    if isinstance(value, str):
        parsed = _coerce_json(value)
        if parsed is not value:
            _collect_interaction_candidates(parsed, result, key)
        elif token_container:
            result.add(value.strip())
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            _collect_interaction_candidates(
                child,
                result,
                key if token_container else str(child_key),
            )
        return
    if isinstance(value, list):
        for child in value:
            _collect_interaction_candidates(child, result, key)


def _candidate_matches(state: dict[str, Any], candidates: set[str]) -> bool:
    expected = {
        str(state.get("confirmation_digest") or ""),
        str(state.get("interaction_digest") or ""),
    }
    expected.discard("")
    return any(
        any(hmac.compare_digest(_challenge_digest(state, candidate), digest) for digest in expected)
        for candidate in candidates
    )


def _challenge_digest(state: dict[str, Any], candidate: str) -> str:
    material = f"{state.get('instance_id') or ''}:{str(candidate or '').strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _message(workflow: WorkflowDefinition | None, key: str) -> str:
    if not workflow:
        return ""
    return _render(str((workflow.definition.get("messages") or {}).get(key) or ""), {})


def workflow_message(workflow_name: str | None, key: str) -> str:
    return _message(load_workflow(workflow_name), key)


def _render(template: str, state: dict[str, Any]) -> str:
    values = _template_values(state)
    return str(template or "").format_map(_MissingValueMap(values)).strip()


def _render_nested(value: Any, state: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render(value, state)
    if isinstance(value, list):
        return [_render_nested(item, state) for item in value]
    if isinstance(value, dict):
        return {key: _render_nested(item, state) for key, item in value.items()}
    return value


def _template_values(state: dict[str, Any]) -> dict[str, Any]:
    values = dict(state.get("collected") or {})
    for key in ("workflow", "origin_message", "confirmation_code", "interaction_token"):
        if key in state:
            values[key] = state.get(key)
    return values


class _MissingValueMap(dict):
    def __missing__(self, key):
        return ""


def _coerce_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return frappe.parse_json(value)
    except Exception:
        return value
