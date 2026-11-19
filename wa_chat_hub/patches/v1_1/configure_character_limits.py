"""Move legacy provider detection into editable settings once."""
import frappe

from wa_chat_hub.ai.prompt_limits import LIMIT_FIELDS


def execute():
    meta = frappe.get_meta("WA LLM Provider")
    defaults = {field: int(meta.get_field(field).default) for field in LIMIT_FIELDS[1:]}
    rows = frappe.get_all(
        "WA LLM Provider",
        fields=["name", "base_url", "model_name", "character_limits_version"],
        limit_page_length=0,
    )
    for row in rows:
        if row.character_limits_version:
            continue
        endpoint = str(row.base_url or "").lower()
        model = str(row.model_name or "").lower()
        previously_limited = "openrouter.ai" in endpoint or "vllm.buopso.net" in endpoint or "glm" in model
        frappe.db.set_value(
            "WA LLM Provider",
            row.name,
            {**defaults, "enable_character_limits": int(previously_limited), "character_limits_version": 1},
            update_modified=False,
        )
