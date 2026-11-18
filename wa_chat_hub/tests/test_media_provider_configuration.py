import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import frappe
import requests
from wa_chat_hub.ai import providers, media_transcription as media, ocr_summary
from wa_chat_hub.ai.provider_endpoints import operation_url
from wa_chat_hub.patches.v1_1 import configure_media_providers as migration


def provider(**overrides):
    return {"name": "Custom Test", "provider_type": "Custom", "base_url": "https://model.test/v1",
            "model_name": "arbitrary-model", "api_key": "test", "transcription_mode": "Multimodal Chat",
            "transcription_model": "", "audio_input_format": "audio_url", **overrides}


class TestProviderEndpoints(TestCase):
    def test_base_full_and_trailing_slash_urls(self):
        for suffix in ("", "/", "/chat/completions", "/chat/completions/", "/audio/transcriptions", "/responses"):
            self.assertEqual(operation_url("https://model.test/v1" + suffix), "https://model.test/v1/chat/completions")
            self.assertEqual(operation_url("https://model.test/v1" + suffix, "transcription"), "https://model.test/v1/audio/transcriptions")

    def test_credentials_query_and_non_http_rejected(self):
        for url in ("https://secret@model.test/v1", "file:///tmp/llm", "https://model.test/v1?key=secret"):
            with self.assertRaises(ValueError):
                operation_url(url)


class TestProviderSelection(TestCase):
    def setUp(self):
        meta = patch.object(providers.frappe, "get_meta", return_value=SimpleNamespace(has_field=lambda name: True))
        meta.start()
        self.addCleanup(meta.stop)

    @patch.object(providers, "safe_ai_get_doc", return_value={"llm_provider": ""})
    @patch.object(providers, "safe_ai_get_all")
    def test_blank_uses_priority_and_explicit_capabilities(self, rows, doc):
        rows.return_value = [frappe._dict(name="one", provider_type="Custom", use_for_chat=1),
                             frappe._dict(name="two", provider_type="Custom", use_for_chat=0)]
        result = providers.get_active_llm_provider_rows(providers.CHAT_CAPABILITY, channel_account="WA")
        self.assertEqual([r.name for r in result], ["one"])
        self.assertEqual(rows.call_args.kwargs["filters"], {"is_active": 1})
        self.assertEqual(rows.call_args.kwargs["order_by"], "priority asc, modified desc")

    @patch.object(providers, "safe_ai_get_doc", return_value={"llm_provider": "chosen"})
    @patch.object(providers, "safe_ai_get_all")
    def test_override_for_all_capabilities(self, rows, doc):
        rows.return_value = [frappe._dict(name="chosen", provider_type="Custom", use_for_chat=1, use_for_vision=1, use_for_transcription=1)]
        for cap in (providers.CHAT_CAPABILITY, providers.VISION_CAPABILITY, providers.TRANSCRIPTION_CAPABILITY):
            self.assertEqual(providers.get_active_llm_provider_rows(cap, channel_account="WA")[0].name, "chosen")
            self.assertEqual(rows.call_args.kwargs["filters"], {"name": "chosen", "is_active": 1})

    @patch.object(providers.frappe, "throw", side_effect=ValueError)
    @patch.object(providers, "safe_ai_get_doc", return_value={"llm_provider": "disabled"})
    @patch.object(providers, "safe_ai_get_all", return_value=[])
    def test_disabled_override_never_falls_back(self, rows, doc, throw):
        with self.assertRaises(ValueError):
            providers.get_active_llm_provider_rows(providers.CHAT_CAPABILITY, channel_account="WA")
        self.assertEqual(rows.call_count, 1)

    @patch.object(providers.frappe, "throw", side_effect=ValueError)
    @patch.object(providers, "safe_ai_get_doc", return_value={"llm_provider": "chosen"})
    @patch.object(providers, "safe_ai_get_all")
    def test_unsupported_override_raises_except_optional_video_component(self, rows, doc, throw):
        rows.return_value = [frappe._dict(name="chosen", provider_type="Custom", use_for_transcription=0)]
        with self.assertRaises(ValueError):
            providers.get_active_llm_provider_rows(providers.TRANSCRIPTION_CAPABILITY, channel_account="WA")
        self.assertEqual(providers.get_active_llm_provider_rows(providers.TRANSCRIPTION_CAPABILITY, channel_account="WA", optional_capability=True), [])

    @patch.object(providers, "safe_ai_get_all")
    def test_unchecked_capability_does_not_guess_from_model(self, rows):
        rows.return_value = [frappe._dict(name="vision", model_name="gpt-4o", provider_type="OpenAI", use_for_vision=0)]
        self.assertEqual(providers.get_active_llm_provider_rows(providers.VISION_CAPABILITY), [])


class TestConfiguredMedia(TestCase):
    def call_transcription(self, p):
        return media._post_transcription(endpoint=operation_url(p['base_url'], "chat" if p['transcription_mode'] == "Multimodal Chat" else "transcription"),
            provider=p, model=media._audio_transcription_model(p), filename="voice.mp3", media_bytes=b"audio",
            mime_type="audio/mpeg", content_type="Audio")

    @patch.object(media.requests, "post")
    def test_multimodal_audio_uses_chat_model_and_base64(self, post):
        post.return_value = Mock(ok=True)
        post.return_value.json.return_value = {"choices": [{"message": {"content": "Hello"}}]}
        self.assertEqual(self.call_transcription(provider()), "Hello")
        self.assertEqual(post.call_args.args[0], "https://model.test/v1/chat/completions")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "arbitrary-model")
        self.assertEqual(payload["messages"][0]["content"][1]["audio_url"]["url"], "data:audio/mpeg;base64,YXVkaW8=")
        self.assertNotIn("files", post.call_args.kwargs)

    @patch.object(media.requests, "post")
    def test_input_audio_and_explicit_model(self, post):
        post.return_value = Mock(ok=True)
        post.return_value.json.return_value = {"choices": [{"message": {"content": "Hello"}}]}
        self.call_transcription(provider(audio_input_format="input_audio", transcription_model="audio-model"))
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload['model'], 'audio-model')
        self.assertEqual(payload["messages"][0]["content"][1]["input_audio"], {"data": "YXVkaW8=", "format": "mp3"})

    @patch.object(media.requests, "post")
    def test_dedicated_transcription_preserves_multipart(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"text": "Hello"}
        self.assertEqual(self.call_transcription(provider(transcription_mode="Transcription API", transcription_model="speech-model")), "Hello")
        self.assertEqual(post.call_args.args[0], "https://model.test/v1/audio/transcriptions")
        self.assertEqual(post.call_args.kwargs["data"]["model"], "speech-model")
        self.assertIn("file", post.call_args.kwargs["files"])

    def test_no_hardcoded_transcription_model(self):
        with self.assertRaises(ValueError):
            media._audio_transcription_model(provider(transcription_mode="Transcription API"))

    @patch.object(media, "_post_transcription")
    @patch.object(media, "_prepare_transcription_upload", return_value={"content": b"video", "mime_type": "audio/mp4", "filename": "video.mp4"})
    @patch.object(media, "_get_audio_transcription_provider", return_value=provider())
    @patch.object(media, "_download_media", return_value={"content": b"video"})
    def test_silent_video_not_uploaded_even_if_legacy_mime_says_audio(self, download, get_provider, upload, post):
        self.assertEqual(media.transcribe_media("https://media.test/a.mp4", "Video", channel_account="WA"), "")
        get_provider.assert_called_once_with("WA", optional_capability=True)
        post.assert_not_called()

    @patch.object(media.frappe, "log_error")
    @patch.object(media, "_post_transcription", side_effect=requests.HTTPError("404"))
    @patch.object(media, "_prepare_transcription_upload", return_value={"content": b"audio", "mime_type": "audio/mpeg", "filename": "video.mp3", "audio_extracted": True})
    @patch.object(media, "_get_audio_transcription_provider", return_value=provider())
    @patch.object(media, "_download_media", return_value={"content": b"video"})
    def test_failed_multimodal_video_not_retried_with_fake_mimes(self, download, get_provider, upload, post, log):
        self.assertEqual(media.transcribe_media("https://media.test/a.mp4", "Video"), "")
        self.assertEqual(post.call_count, 1)

    @patch.object(media, "policy_section", return_value={})
    @patch.object(media, "describe_video_media", return_value="A red circle moves right")
    def test_video_combines_audio_and_visual_context(self, describe, policy):
        context = media.build_transcript_context_for_chat("https://media.test/a.mp4", "Video", body_hint="My video", channel_account="WA", transcript="Appointment on Friday")
        for text in ['A red circle moves right','Appointment on Friday','My video']:
            self.assertIn(text, context)
        self.assertEqual(describe.call_args.kwargs['channel_account'], 'WA')

    @patch.object(media, "policy_section", return_value={})
    @patch.object(media, "describe_video_media", return_value="A red circle")
    def test_silent_video_retains_visual_context(self, describe, policy):
        self.assertIn('A red circle', media.build_transcript_context_for_chat('video', 'Video', transcript=''))

    @patch.object(media, "policy_section", return_value={})
    @patch.object(media, "describe_video_media", return_value="")
    def test_failed_visuals_retain_audio(self, describe, policy):
        self.assertIn('Appointment on Friday', media.build_transcript_context_for_chat('video', 'Video', transcript='Appointment on Friday'))

    @patch.object(media, "policy_reply", return_value="Please resend the video")
    @patch.object(media, "policy_section", return_value={})
    @patch.object(media, "describe_video_media", return_value="")
    def test_neither_component_uses_existing_policy_response(self, describe, policy, reply):
        self.assertIn('Please resend the video', media.build_transcript_context_for_chat('video', 'Video', transcript=''))

    @patch.object(media, 'get_provider_secret', return_value=provider())
    @patch.object(media, 'get_active_llm_provider_rows')
    def test_frames_require_opt_in_or_legacy_policy(self, rows, secret):
        rows.return_value=[frappe._dict(name='one',analyse_video_frames=0)]
        self.assertEqual(media._get_vision_providers('WA',video_frames=True), [])
        secret.assert_not_called()
        self.assertEqual(len(media._get_vision_providers('WA',video_frames=True,legacy_frames_enabled=True)),1)

    @patch.object(ocr_summary, '_get_openai_compatible_providers', return_value=[])
    def test_ocr_receives_channel(self, rows):
        ocr_summary._extract_with_openai_vision('https://media.test/a.png',channel_account='WA')
        rows.assert_called_once_with(require_vision=True,channel_account='WA')


class TestMediaMigration(TestCase):
    @patch.object(migration, 'frappe')
    def test_legacy_defaults_explicit_once(self, mock_frappe):
        mock_frappe.get_all.return_value=[frappe._dict(name='legacy',provider_type='OpenAI',model_name='gpt-4o-mini',is_active=1),
                                         frappe._dict(name='configured',provider_type='Custom',is_active=0,media_settings_version=1)]
        migration.execute()
        self.assertEqual(mock_frappe.db.set_value.call_count,1)
        values=mock_frappe.db.set_value.call_args.args[2]
        self.assertEqual(values['transcription_model'],'whisper-1')
        self.assertEqual(values['use_for_transcription'],1)
        self.assertEqual(values['media_settings_version'],1)
