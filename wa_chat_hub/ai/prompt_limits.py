"""Provider-controlled character budgets for chat requests (not token limits)."""
from copy import deepcopy
from dataclasses import dataclass
import json


LIMIT_FIELDS = (
    "enable_character_limits",
    "max_system_prompt_chars",
    "max_input_chars",
    "max_message_chars",
)


@dataclass(frozen=True)
class CharacterLimits:
    system: int
    total: int
    message: int


def get_character_limits(provider):
    if not int(getattr(provider, "enable_character_limits", 0) or 0):
        return None
    values = []
    for field in LIMIT_FIELDS[1:]:
        value = getattr(provider, field, None)
        try:
            number = int(str(value))
        except (TypeError, ValueError):
            raise ValueError(f"{field}: enter a positive whole number.") from None
        if number <= 0:
            raise ValueError(f"{field}: enter a positive whole number.")
        values.append(number)
    limits = CharacterLimits(*values)
    if limits.total <= limits.system:
        raise ValueError("Maximum Total Message Characters must exceed Maximum System Prompt Characters.")
    if limits.message > limits.total:
        raise ValueError("Maximum Characters per Message cannot exceed Maximum Total Message Characters.")
    return limits


def shorten_text(text, limit):
    if len(text) <= limit:
        return text
    marker = "\n[Middle shortened for provider character limit.]\n"
    if limit <= len(marker):
        return text[:limit]
    available = limit - len(marker)
    head = int(available * 0.65)
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _content_size(content):
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(part.get("text", "")) for part in content if isinstance(part, dict))
    return 0


def _trim_content(content, limit):
    if isinstance(content, str):
        return shorten_text(content, limit)
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part["text"] = shorten_text(part["text"], limit)
                limit -= len(part["text"])
    return content


def _call_size(message):
    # Function names and arguments cannot be shortened without changing the call.
    calls = message.get("tool_calls")
    return len(json.dumps(calls, ensure_ascii=False)) if calls else 0


def message_characters(message):
    return _content_size(message.get("content")) + _call_size(message)


def _fit_turn(turn, budget):
    fixed = sum(_call_size(message) for message in turn)
    sizes = [_content_size(message.get("content")) for message in turn]
    available = budget - fixed
    if available < sum(bool(size) for size in sizes):
        raise ValueError("Provider character budget is too small for the latest turn and its tool calls.")
    remaining_size = sum(sizes)
    for index, (message, size) in enumerate(zip(turn, sizes)):
        if size:
            # Reserve at least one character for every later nonempty message.
            later = sum(bool(n) for n in sizes[index + 1:])
            allowance = min(size, available - later, max(1, available * size // remaining_size))
            message["content"] = _trim_content(message.get("content"), allowance)
            available -= _content_size(message.get("content"))
            remaining_size -= size
    return turn


def fit_messages(provider, messages):
    """Budget each request independently; never alter source history or images.

    Keep the latest user turn and its assistant/tool messages together. Older
    turns are dropped as units, so no orphaned tool results reach the provider.
    Image payloads and tool schemas are outside this text-only budget.
    """
    fitted = deepcopy(messages)
    limits = get_character_limits(provider)
    if not limits or not fitted:
        return fitted
    system = []
    system_remaining = limits.system
    turns = []
    for message in fitted:
        if message.get("role") in ("system", "developer"):
            message["content"] = _trim_content(message.get("content"), system_remaining)
            system_remaining -= _content_size(message.get("content"))
            system.append(message)
            continue
        if not turns or message.get("role") == "user":
            turns.append([])
        message["content"] = _trim_content(message.get("content"), limits.message)
        turns[-1].append(message)
    remaining = limits.total - sum(message_characters(message) for message in system)
    kept = []
    for turn in reversed(turns):
        size = sum(message_characters(message) for message in turn)
        if size > remaining:
            if kept:
                break
            turn = _fit_turn(turn, remaining)
            size = sum(message_characters(message) for message in turn)
        kept.insert(0, turn)
        remaining -= size
    return system + [message for turn in kept for message in turn]
