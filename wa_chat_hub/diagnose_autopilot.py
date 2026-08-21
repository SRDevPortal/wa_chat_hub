"""Run: bench --site localhost execute wa_chat_hub.diagnose_autopilot.run"""

from __future__ import annotations

import frappe


def run():
    from wa_chat_hub.prompts import build_system_prompt_from_config, get_effective_prompt_config

    s = frappe.get_single("WA Chat Hub Settings")
    print("enable_ai_autopilot:", int(s.enable_ai_autopilot or 0))
    print("autopilot_mode:", s.autopilot_mode)
    print("sends_whatsapp:", int(s.enable_ai_autopilot) and s.autopilot_mode == "Limited Auto Reply")
    global_prompt_len = len(build_system_prompt_from_config(s))
    print("global_system_prompt_chars:", global_prompt_len)
    if not global_prompt_len:
        print("WARN: Global System Prompt is empty — autopilot will not reply.")

    for row in s.get("account_prompt_maps") or []:
        cfg = get_effective_prompt_config(row.chat_channel_account)
        plen = len(build_system_prompt_from_config(cfg))
        print(
            f"account_map: {row.chat_channel_account} active={row.is_active} "
            f"merged_prompt_chars={plen}"
        )

    providers = frappe.get_all(
        "WA LLM Provider", filters={"is_active": 1}, fields=["name", "provider_type", "model_name"]
    )
    print("active_providers:", len(providers))
    for p in providers:
        d = frappe.get_doc("WA LLM Provider", p.name)
        print(" -", p.name, p.provider_type, "api_key:", bool(d.get_password("api_key")))

    try:
        from wa_chat_hub.api.vector_search import search_knowledge_base

        print("vector_search_import: OK")
        print("kb_search_sample:", len(search_knowledge_base("shipping rates", top_k=1)))
    except Exception as exc:
        print("vector_search_import: FAIL", exc)

    from frappe.utils import add_to_date, now_datetime

    since = add_to_date(now_datetime(), hours=-2, as_datetime=True)
    errors = frappe.get_all(
        "Error Log",
        filters={
            "creation": [">", since],
            "error": ["like", "%wa_chat_hub%"],
        },
        fields=["name", "creation", "method", "error"],
        order_by="creation desc",
        limit=5,
    )
    print("recent_wa_chat_errors_last_2h:", len(errors))
    if not errors:
        print("  (none — good; older May-20 errors are from before TimestampMismatch fix)")
    for e in errors:
        err_text = frappe.db.get_value("Error Log", e.name, "error") or ""
        print(" ", e.creation, e.method)
        if "convo.save" in err_text and "update_conversation_after_message" in err_text:
            print("  WARN: old convo.save bug — restart bench worker after code update")
        print(err_text[:800])
        print("---")

    pending = frappe.get_all(
        "RQ Job",
        filters={"status": "queued", "job_name": ["like", "%process_message%"]},
        fields=["name", "creation"],
        limit=5,
    )
    print("queued_process_message_jobs:", len(pending))
    print(
        "NOTE: Autopilot always enqueues async (now=False). "
        "Run `bench worker` or `bench schedule` — developer_mode no longer runs AI inline."
    )
    try:
        from wa_chat_hub.services import update_conversation_after_message
        import inspect

        src = inspect.getsource(update_conversation_after_message)
        if "frappe.db.set_value" in src and "convo.save" not in src:
            print("code_check: update_conversation_after_message uses set_value OK")
        else:
            print("code_check: FAIL — restart bench / pull latest wa_chat_hub (still uses convo.save)")
    except Exception as exc:
        print("code_check: error", exc)
    print("run_e2e: bench --site localhost execute wa_chat_hub.test_autopilot_e2e.run")
