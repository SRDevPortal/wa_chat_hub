"""Persist legacy transcription defaults and capability inference once."""
import frappe
from wa_chat_hub.ai.providers import looks_like_transcription_model, looks_like_transcription_provider, looks_like_vision_model


def execute():
    rows = frappe.get_all("WA LLM Provider", fields=["*"], limit_page_length=0)
    active = [r for r in rows if r.is_active and r.provider_type in {"OpenAI", "Gemini", "Custom"}]
    caps = ("use_for_chat", "use_for_vision", "use_for_transcription")
    explicit = {cap: any(r.get(cap) for r in active) for cap in caps}
    for row in rows:
        if row.get("media_settings_version") == 1:
            continue
        values = {"transcription_mode": "Transcription API", "audio_input_format": "audio_url",
                  "transcription_model": row.model_name if looks_like_transcription_model(row.model_name) else "whisper-1",
                  "analyse_video_frames": 0, "media_settings_version": 1}
        inferred = {"use_for_chat": not looks_like_transcription_model(row.model_name, row.base_url),
                    "use_for_vision": looks_like_vision_model(row.provider_type, row.model_name),
                    "use_for_transcription": looks_like_transcription_provider(row.provider_type, row.model_name, row.base_url)}
        for cap in caps:
            if not explicit[cap]:
                values[cap] = int(inferred[cap])
        frappe.db.set_value("WA LLM Provider", row.name, values, update_modified=False)
