# Provider selection and audio/video configuration

## Local setup

The settings are installed on `site1.local`. Refresh Desk and open **WA LLM Provider > gemma4**.

| Setting | Local value |
| --- | --- |
| Base URL | `https://vllm.buopso.net/v1` |
| Model Name | `gemma4-e4b` |
| Transcription Mode | Multimodal Chat |
| Transcription Model | Blank (uses Model Name) |
| Audio Input Format | audio_url |
| Analyse Video Frames | Enabled |
| Use for Vision / OCR | Enabled |

The existing API key is reused. No secret is stored in this guide. Existing active status and priority are preserved.

## Provider switching

Without a channel override, active providers are selected by capability and ascending Priority. Disable providers that should no longer be used. Existing chat fallback can try other eligible defaults.

**Chat Channel Account > LLM Provider** is optional. A selection confines chat, speech transcription, image analysis and video processing to that provider. Blank follows the priority list. This supersedes the older agent-level provider preference. A disabled, incompatible or unsupported explicit selection raises a configuration error, without switching to another account. Optional video components can be disabled: a channel with only audio enabled can still transcribe video; one with only vision enabled can describe silent video.

These integrations use the OpenAI-compatible API protocol. Other native API protocols require an adapter.

## Transcription modes

**Transcription API** uploads an audio file to `/audio/transcriptions`. Set Transcription Model explicitly, for example `whisper-1` for an account serving that model. Existing provider defaults are recorded by a one-time migration, rather than guessed at runtime.

**Multimodal Chat** sends encoded audio with a transcription instruction to `/chat/completions`. A blank Transcription Model uses Model Name. Select `audio_url` or `input_audio` according to the server's supported input format. Both use the saved Base URL and API Key. A base URL or a legacy full operation URL is accepted.

## Video flow

1. Extract speech audio with ffmpeg and transcribe through the selected mode.
2. When Analyse Video Frames is enabled, extract the first frame and sample at four-second intervals, up to four frames. Send them to the vision-capable model.
3. Include the speech transcript, visual description and caption as context for the existing reply pipeline.

Frame sampling does not inspect the entire video and may miss brief events. Silent videos retain visual context. If one component's processing fails, retain the usable component; if both are unavailable, use the existing media-unavailable policy response. Misconfigured provider selection is surfaced rather than redirected silently.

For compatibility, channels with the older `enable_local_clinical_heuristics` media-policy flag continue requesting provider-based frame analysis. Turn that flag off if disabling frame analysis for those existing channels.

## Installation and testing

Standard deployment uses `bench --site <site> migrate`. This local site's two DocTypes and migration have already been applied; no remote environment was updated.

Focused tests cover selection, overrides, migration, both request formats, dedicated transcription, partial video failures and existing routing/security behavior. Synthetic live tests against the configured custom server verify audio transcription, video speech and frames, a one-second silent video, and automatic tool calling. No customer messages are sent by these tests. Dedicated transcription is verified with mocked HTTP responses; no live OpenAI request was made.
