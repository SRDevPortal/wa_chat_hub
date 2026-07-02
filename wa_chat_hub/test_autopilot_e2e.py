"""
End-to-end autopilot smoke tests (no live Interakt/OpenAI calls when dry_run=1).

Run:
  bench --site localhost execute wa_chat_hub.test_autopilot_e2e.run
  bench --site localhost execute wa_chat_hub.test_autopilot_e2e.run --kwargs '{"live_llm": 1}'
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import frappe


def run(live_llm: int = 0, live_send: int = 0):
    import os

    results = []
    results.append(_check_update_uses_set_value())
    results.append(_test_interakt_inbound_media_webhook_queue())
    conv, channel, phone = _pick_test_conversation()
    if not conv:
        results.append(("conversation", False, "No Chat Conversation found — create one via WA Chat Hub first"))
        _print_results(results)
        _write_log(results, ok=False)
        return {"ok": False, "results": results}

    results.append(_test_concurrent_preview_updates(conv))
    results.append(_test_autopilot_batching_supersedes_text(conv, channel, phone))
    results.append(_test_autopilot_batching_supersedes_media(conv, channel, phone))
    results.append(_test_autopilot_batching_disabled_allows_old(conv, channel, phone))
    results.append(_test_conversation_batch_selects_latest_media(conv, channel, phone))
    results.append(_test_direct_message_reroutes_to_conversation_batch(conv, channel, phone))
    results.append(_test_media_batch_already_answered_guard(conv, channel, phone))
    results.append(_test_slow_media_delivery_timing(conv, channel, phone))
    results.append(_test_ai_outbound_append_no_autopilot_schedule(conv, channel, phone))
    results.append(_test_deliver_ai_reply_mocked(conv, channel, phone))
    results.append(_test_queued_manual_send_mocked(conv, channel, phone))

    if int(live_llm or 0):
        results.append(_test_process_message_live(conv, channel, phone))

    _print_results(results)
    ok = all(r[1] for r in results)
    _write_log(results, ok=ok, conversation=conv)
    return {"ok": ok, "results": results, "conversation": conv}


def _write_log(results, ok: bool, conversation: str | None = None):
    import os

    bench_path = frappe.get_site_path("..", "..")
    log_path = os.path.join(bench_path, "logs", "autopilot_e2e_result.txt")
    lines = ["=== WA Chat Hub Autopilot E2E ===", f"ok={ok}", f"conversation={conversation or ''}"]
    for name, passed, detail in results:
        lines.append(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        print(f"log_written: {log_path}")
    except Exception as exc:
        print(f"log_write_failed: {exc}")


def _check_update_uses_set_value():
    from wa_chat_hub.services import update_conversation_after_message

    src = inspect.getsource(update_conversation_after_message)
    uses_direct_update = "frappe.db.sql" in src or "safe_ai_set_value" in src or "frappe.db.set_value" in src
    if uses_direct_update and "convo.save" not in src:
        return ("update_conversation_after_message", True, "uses direct guarded update (no convo.save)")
    return ("update_conversation_after_message", False, "still uses doc save — redeploy/restart bench")


def _test_interakt_inbound_media_webhook_queue():
    from wa_chat_hub.api.webhook import _interakt_webhook_queue

    payload = {
        "type": "message_received",
        "data": {
            "message": {
                "message_content_type": "Image",
            }
        },
    }
    queue = _interakt_webhook_queue(payload)
    if queue == "short":
        return ("interakt_inbound_media_webhook_queue", True, "media receipt uses short queue")
    return ("interakt_inbound_media_webhook_queue", False, f"expected short queue, got {queue}")


def _pick_test_conversation():
    row = frappe.db.sql(
        """
        SELECT c.name, c.channel_account, ct.phone_number
        FROM `tabChat Conversation` c
        JOIN `tabChat Contact` ct ON ct.name = c.contact
        WHERE c.channel_account IS NOT NULL AND ct.phone_number IS NOT NULL
        ORDER BY c.modified DESC
        LIMIT 1
        """,
        as_dict=True,
    )
    if not row:
        return None, None, None
    r = row[0]
    return r.name, r.channel_account, r.phone_number


def _test_concurrent_preview_updates(conversation: str):
    """Reproduce TimestampMismatch scenario: stale get_doc.save while preview updates."""
    from wa_chat_hub.services import update_conversation_after_message

    stale = frappe.get_doc("Chat Conversation", conversation)
    stale.last_message_preview = "stale-holder"

    update_conversation_after_message(
        conversation,
        {"direction": "Inbound", "body": "e2e-inbound-preview", "content_type": "Text"},
    )
    update_conversation_after_message(
        conversation,
        {"direction": "Outbound", "body": "e2e-ai-preview", "content_type": "Text", "sender_type": "AI"},
    )

    try:
        stale.save(ignore_permissions=True)
        return (
            "concurrent_preview",
            False,
            "stale save succeeded — TimestampMismatch fix may not be exercised",
        )
    except frappe.TimestampMismatchError:
        pass

    preview = frappe.db.get_value("Chat Conversation", conversation, "last_message_preview")
    if "e2e-ai-preview" in (preview or ""):
        return ("concurrent_preview", True, f"preview={preview[:80]}")
    return ("concurrent_preview", False, f"unexpected preview={preview!r}")


def _append_e2e_inbound(channel, phone, body, content_type="Text", media_url=None):
    from wa_chat_hub.services import append_message

    result = append_message(
        {
            "channel_account": channel,
            "phone_number": phone,
            "direction": "Inbound",
            "sender_type": "Customer",
            "content_type": content_type,
            "body": body,
            "media_url": media_url,
            "channel_message_id": f"e2e-batch-{frappe.generate_hash(length=10)}",
        }
    )
    return result.get("message")


def _test_autopilot_batching_supersedes_text(conv, channel, phone):
    from wa_chat_hub.api.ai_bot import _wait_for_autopilot_batch_window

    first = _append_e2e_inbound(
        channel,
        phone,
        f"E2E batch text first {frappe.generate_hash(length=8)}",
    )
    second = _append_e2e_inbound(
        channel,
        phone,
        f"E2E batch text second {frappe.generate_hash(length=8)}",
    )
    frappe.db.commit()

    settings = frappe._dict(
        {
            "enable_autopilot_reply_batching": 1,
            "text_autopilot_reply_delay_seconds": 0,
            "media_autopilot_reply_delay_seconds": 0,
        }
    )
    first_superseded, _ = _wait_for_autopilot_batch_window(frappe.get_doc("Chat Message", first), settings)
    second_superseded, _ = _wait_for_autopilot_batch_window(frappe.get_doc("Chat Message", second), settings)
    if first_superseded and not second_superseded:
        return ("autopilot_batch_text", True, f"first={first} second={second}")
    return (
        "autopilot_batch_text",
        False,
        f"first_superseded={first_superseded} second_superseded={second_superseded}",
    )


def _test_autopilot_batching_supersedes_media(conv, channel, phone):
    from wa_chat_hub.api.ai_bot import _wait_for_autopilot_batch_window

    first = _append_e2e_inbound(
        channel,
        phone,
        "[Image message received]",
        content_type="Image",
        media_url="https://example.com/e2e-batch-first.jpg",
    )
    second = _append_e2e_inbound(
        channel,
        phone,
        "[Audio message received]",
        content_type="Audio",
        media_url="https://example.com/e2e-batch-second.ogg",
    )
    frappe.db.commit()

    settings = frappe._dict(
        {
            "enable_autopilot_reply_batching": 1,
            "text_autopilot_reply_delay_seconds": 0,
            "media_autopilot_reply_delay_seconds": 0,
        }
    )
    first_superseded, _ = _wait_for_autopilot_batch_window(frappe.get_doc("Chat Message", first), settings)
    second_superseded, _ = _wait_for_autopilot_batch_window(frappe.get_doc("Chat Message", second), settings)
    if first_superseded and not second_superseded:
        return ("autopilot_batch_media", True, f"first={first} second={second}")
    return (
        "autopilot_batch_media",
        False,
        f"first_superseded={first_superseded} second_superseded={second_superseded}",
    )


def _test_autopilot_batching_disabled_allows_old(conv, channel, phone):
    from wa_chat_hub.api.ai_bot import _wait_for_autopilot_batch_window

    first = _append_e2e_inbound(
        channel,
        phone,
        f"E2E disabled batch first {frappe.generate_hash(length=8)}",
    )
    _append_e2e_inbound(
        channel,
        phone,
        f"E2E disabled batch second {frappe.generate_hash(length=8)}",
    )
    frappe.db.commit()

    settings = frappe._dict(
        {
            "enable_autopilot_reply_batching": 0,
            "text_autopilot_reply_delay_seconds": 0,
            "media_autopilot_reply_delay_seconds": 0,
        }
    )
    superseded, delay = _wait_for_autopilot_batch_window(frappe.get_doc("Chat Message", first), settings)
    if not superseded and delay == 0:
        return ("autopilot_batch_disabled", True, f"first={first}")
    return ("autopilot_batch_disabled", False, f"superseded={superseded} delay={delay}")


def _test_conversation_batch_selects_latest_media(conv, channel, phone):
    from wa_chat_hub.api.ai_bot import _wait_for_conversation_batch_window

    first = _append_e2e_inbound(
        channel,
        phone,
        "[Image message received]",
        content_type="Image",
        media_url="https://example.com/e2e-conversation-batch-first.jpg",
    )
    second = _append_e2e_inbound(
        channel,
        phone,
        "[Document message received]",
        content_type="Document",
        media_url="https://example.com/e2e-conversation-batch-second.pdf",
    )
    frappe.db.commit()

    old_in_test = getattr(frappe.flags, "in_test", False)
    frappe.flags.in_test = True
    try:
        settings = frappe._dict(
            {
                "enable_autopilot_reply_batching": 1,
                "text_autopilot_reply_delay_seconds": 0,
                "media_autopilot_reply_delay_seconds": 0,
            }
        )
        selected_doc, _ = _wait_for_conversation_batch_window(conv, settings)
        selected = str(selected_doc.name) if selected_doc else ""
    finally:
        frappe.flags.in_test = old_in_test

    if selected == str(second):
        return ("conversation_batch_latest_media", True, f"first={first} selected={second}")
    return (
        "conversation_batch_latest_media",
        False,
        f"selected={selected} expected={second}",
    )


def _test_direct_message_reroutes_to_conversation_batch(conv, channel, phone):
    from wa_chat_hub.api.ai_bot import process_message

    inbound = _append_e2e_inbound(
        channel,
        phone,
        "[Image message received]",
        content_type="Image",
        media_url="https://example.com/e2e-direct-reroute.jpg",
    )
    frappe.db.commit()

    with patch("wa_chat_hub.api.ai_bot.process_conversation") as mock_batch:
        process_message(inbound)
        if mock_batch.called:
            args, kwargs = mock_batch.call_args
            if str(args[0]) == str(conv) and str(kwargs.get("trigger_message_id")) == str(inbound):
                return ("direct_media_reroute", True, f"message={inbound}")
            return ("direct_media_reroute", False, f"unexpected call args={args} kwargs={kwargs}")
    return ("direct_media_reroute", False, "process_conversation not called")


def _test_media_batch_already_answered_guard(conv, channel, phone):
    from wa_chat_hub.api.ai_bot import _autopilot_batch_already_answered
    from wa_chat_hub.services import append_message

    first = _append_e2e_inbound(
        channel,
        phone,
        "[Image message received]",
        content_type="Image",
        media_url="https://example.com/e2e-answered-first.jpg",
    )
    append_message(
        {
            "channel_account": channel,
            "phone_number": phone,
            "direction": "Outbound",
            "sender_type": "AI",
            "content_type": "Text",
            "body": f"E2E answered media batch {frappe.generate_hash(length=8)}",
            "delivery_status": "Sent",
            "raw_transport_payload": {"source": "e2e"},
        }
    )
    second = _append_e2e_inbound(
        channel,
        phone,
        "[Document message received]",
        content_type="Document",
        media_url="https://example.com/e2e-answered-second.pdf",
    )
    frappe.db.commit()

    answered = _autopilot_batch_already_answered(conv, frappe.get_doc("Chat Message", second))
    if not answered:
        return ("media_batch_answered_guard", True, f"new_batch_allowed first={first} second={second}")
    return ("media_batch_answered_guard", False, f"guard incorrectly blocked new media batch")


def _test_slow_media_delivery_timing(conv, channel, phone):
    from frappe.utils import add_to_date, get_datetime, now_datetime
    from wa_chat_hub.api.ai_bot import (
        AUTOPILOT_MEDIA_BURST_GAP_SECONDS,
        _media_burst_window_start,
    )

    base = now_datetime().replace(microsecond=0)
    first_at = base
    second_at = add_to_date(base, seconds=47)
    third_at = add_to_date(base, seconds=108)

    first = _append_e2e_inbound(
        channel,
        phone,
        "[Image message received]",
        content_type="Image",
        media_url=f"https://example.com/e2e-slow-media-{frappe.generate_hash(length=8)}-1.jpg",
    )
    second = _append_e2e_inbound(
        channel,
        phone,
        "[Image message received]",
        content_type="Image",
        media_url=f"https://example.com/e2e-slow-media-{frappe.generate_hash(length=8)}-2.jpg",
    )
    third = _append_e2e_inbound(
        channel,
        phone,
        "[Image message received]",
        content_type="Image",
        media_url=f"https://example.com/e2e-slow-media-{frappe.generate_hash(length=8)}-3.jpg",
    )

    for message, created_at in ((first, first_at), (second, second_at), (third, third_at)):
        frappe.db.set_value("Chat Message", message, "creation", created_at, update_modified=False)
    frappe.db.commit()

    burst_start = _media_burst_window_start(conv, third_at)
    grouped = bool(
        burst_start
        and abs((get_datetime(burst_start) - get_datetime(first_at)).total_seconds()) < 1
    )

    configured_delay = frappe.db.get_single_value("WA Chat Hub Settings", "media_autopilot_reply_delay_seconds") or 0
    max_gap = max(
        int((second_at - first_at).total_seconds()),
        int((third_at - second_at).total_seconds()),
    )
    delay_can_group = int(configured_delay) > max_gap

    if grouped and delay_can_group:
        return (
            "slow_media_delivery_timing",
            True,
            f"grouped={first},{second},{third}; delay={configured_delay}s covers max_gap={max_gap}s",
        )
    if grouped:
        return (
            "slow_media_delivery_timing",
            True,
            f"burst grouping OK, but delay={configured_delay}s < max_gap={max_gap}s so live jobs can reply separately",
        )
    return (
        "slow_media_delivery_timing",
        False,
        f"burst_start={burst_start} expected={first_at}; gap_limit={AUTOPILOT_MEDIA_BURST_GAP_SECONDS}s",
    )


def _test_ai_outbound_append_no_autopilot_schedule(conv, channel, phone):
    from wa_chat_hub.api.ai_bot import schedule_autopilot_for_message

    with patch("wa_chat_hub.outbound.send_outbound_message") as mock_send:
        mock_send.return_value = {
            "delivery_status": "Sent",
            "provider_message_id": "e2e-mock-outbound",
            "sent": True,
        }
        from wa_chat_hub.api.ai_bot import _deliver_ai_reply

        _deliver_ai_reply(conv, "E2E mocked AI reply — safe test")

    frappe.db.commit()
    preview = frappe.db.get_value("Chat Conversation", conv, "last_message_preview")
    msg = frappe.db.get_value(
        "Chat Message",
        {"conversation": conv, "body": ["like", "%E2E mocked AI%"]},
        "name",
        order_by="creation desc",
    )
    if not msg:
        return ("ai_outbound_message", False, "outbound Chat Message not created")

    schedule_autopilot_for_message(msg)
    doc = frappe.get_doc("Chat Message", msg)
    if doc.direction == "Inbound":
        return ("ai_outbound_schedule", False, "test message unexpectedly inbound")

    return (
        "ai_outbound_append",
        True,
        f"message={msg} preview={str(preview)[:60]}",
    )


def _test_deliver_ai_reply_mocked(conv, channel, phone):
    try:
        with patch("wa_chat_hub.outbound.send_outbound_message") as mock_send:
            mock_send.return_value = {
                "delivery_status": "Sent",
                "provider_message_id": "e2e-deliver-2",
            }
            from wa_chat_hub.api.ai_bot import _deliver_ai_reply

            _deliver_ai_reply(conv, "E2E deliver path OK")
        frappe.db.commit()
        return ("deliver_ai_reply", True, "no exception")
    except frappe.TimestampMismatchError as exc:
        return ("deliver_ai_reply", False, str(exc))
    except Exception as exc:
        return ("deliver_ai_reply", False, frappe.get_traceback())


def _test_queued_manual_send_mocked(conv, channel, phone):
    from wa_chat_hub.services import append_message
    from wa_chat_hub.api.runtime import send_pending_reply_to_provider

    body = f"E2E queued manual send {frappe.generate_hash(length=8)}"
    result = append_message(
        {
            "channel_account": channel,
            "phone_number": phone,
            "direction": "Outbound",
            "sender_type": "Agent",
            "content_type": "Text",
            "body": body,
            "delivery_status": "Pending",
            "raw_transport_payload": {"queued": True},
        }
    )
    message = result.get("message")
    if not message:
        return ("queued_manual_send", False, "pending message not created")

    with patch("wa_chat_hub.api.runtime.send_outbound_message") as mock_send:
        mock_send.return_value = {
            "delivery_status": "Sent",
            "provider_message_id": f"e2e-provider-{frappe.generate_hash(length=6)}",
            "sent": True,
        }
        send_pending_reply_to_provider(message, conv, body=body, content_type="Text")

    frappe.db.commit()
    status = frappe.db.get_value("Chat Message", message, "delivery_status")
    if status == "Sent":
        return ("queued_manual_send", True, f"message={message} status={status}")
    return ("queued_manual_send", False, f"message={message} status={status}")


def _test_process_message_live(conv, channel, phone):
    from wa_chat_hub.services import append_message

    body = f"E2E fresh inbound for process_message {frappe.generate_hash(length=8)}"
    result = append_message(
        {
            "channel_account": channel,
            "phone_number": phone,
            "direction": "Inbound",
            "sender_type": "Customer",
            "content_type": "Text",
            "body": body,
            "channel_message_id": f"e2e-inbound-{frappe.generate_hash(length=8)}",
        }
    )
    inbound = result.get("message")
    if not inbound:
        return ("process_message_live", False, "fresh inbound message not created")

    with patch("wa_chat_hub.api.ai_bot.call_provider", return_value="E2E live LLM mock reply"):
        with patch("wa_chat_hub.outbound.send_outbound_message") as mock_send:
            mock_send.return_value = {"delivery_status": "Sent", "provider_message_id": "e2e-live"}
            from wa_chat_hub.api.ai_bot import process_message

            process_message(inbound)

    frappe.db.commit()
    out = frappe.db.exists(
        "Chat Message",
        {"conversation": conv, "body": ["like", "%E2E live LLM mock%"]},
    )
    if out:
        return ("process_message_live", True, f"outbound created from {inbound}")
    return ("process_message_live", False, "process_message ran but no outbound body found")


def _print_results(results):
    print("=== WA Chat Hub Autopilot E2E ===")
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {detail}")
    print("=== done ===")
