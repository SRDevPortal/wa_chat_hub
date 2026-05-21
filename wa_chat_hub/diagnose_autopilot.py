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

    errors = frappe.get_all(
        "Error Log",
        filters={"error": ["like", "%WA AI%"]},
        fields=["name", "creation", "error"],
        order_by="creation desc",
        limit=5,
    )
    print("recent_ai_errors:", len(errors))
    for e in errors:
        print(" ", e.creation, (e.error or "")[:100])

    pending = frappe.get_all(
        "RQ Job",
        filters={"status": "queued", "job_name": ["like", "%process_message%"]},
        fields=["name", "creation"],
        limit=5,
    )
    print("queued_process_message_jobs:", len(pending))
