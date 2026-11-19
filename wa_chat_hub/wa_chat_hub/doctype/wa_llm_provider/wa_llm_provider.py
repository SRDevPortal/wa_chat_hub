import frappe
from frappe.model.document import Document
from wa_chat_hub.ai.prompt_limits import get_character_limits
from wa_chat_hub.ai.provider_endpoints import operation_url


class WALLMProvider(Document):
    def validate(self):
        try:
            get_character_limits(self)
        except ValueError as exc:
            frappe.throw(str(exc))
        self.character_limits_version = 1
        if self.base_url:
            try:
                operation_url(self.base_url)
            except ValueError as exc:
                frappe.throw(str(exc))
        if self.use_for_transcription and self.transcription_mode not in {"Transcription API", "Multimodal Chat"}:
            frappe.throw("Select a supported Transcription Mode.")
        if self.use_for_transcription and self.transcription_mode == "Multimodal Chat" and self.audio_input_format not in {"audio_url", "input_audio"}:
            frappe.throw("Select Audio Input Format: audio_url or input_audio.")
        if self.use_for_transcription and self.transcription_mode == "Transcription API" and not str(self.transcription_model or "").strip():
            frappe.throw("Set Transcription Model, or choose Multimodal Chat to use Model Name.")
        if self.analyse_video_frames and not self.use_for_vision:
            frappe.throw("Enable Use for Vision / OCR before Analyse Video Frames.")
        self.media_settings_version = 1
