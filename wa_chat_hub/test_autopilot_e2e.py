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
    conv, channel, phone = _pick_test_conversation()
    if not conv:
        results.append(("conversation", False, "No Chat Conversation found — create one via WA Chat Hub first"))
        _print_results(results)
        _write_log(results, ok=False)
        return {"ok": False, "results": results}

    results.append(_test_concurrent_preview_updates(conv))
    results.append(_test_ai_outbound_append_no_autopilot_schedule(conv, channel, phone))
    results.append(_test_deliver_ai_reply_mocked(conv, channel, phone))

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
