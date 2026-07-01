from __future__ import annotations

import base64
from datetime import datetime
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
from typing import Dict, Optional

import frappe
import requests

from wa_chat_hub.ai.providers import (
    CHAT_CAPABILITY,
    TRANSCRIPTION_CAPABILITY,
    VISION_CAPABILITY,
    get_active_llm_provider_rows,
    get_provider_secret,
    looks_like_vision_model,
)
from wa_chat_hub.prompts import get_conversation_crm_lead


TRANSCRIPT_NOTE_SEPARATOR = "-" * 36
SR_LEAD_NOTES_MAX_LEN = 6000
TRANSCRIPT_CONTENT_TYPES = {"Audio", "Video"}


def build_transcript_context_for_chat(
    media_url: str,
    content_type: str,
    body_hint: str = "",
) -> str:
    """Plain-text transcript context for inbound audio/video autopilot replies."""
    content_type = str(content_type or "Audio").title()
    caption = _clean_caption(body_hint)
    label = "voice note" if content_type == "Audio" else "video"
    lines = [f"Customer sent a WhatsApp {label}."]
    if caption:
        lines.append(f"Caption: {caption}")

    visual_summary = describe_video_media(media_url) if content_type == "Video" else ""
    if visual_summary:
        lines.append(f"Visible video content:\n{visual_summary[:3500]}")

    transcript = transcribe_media(media_url, content_type)
    if transcript:
        label = "Spoken transcript" if content_type == "Video" else f"{content_type} transcript"
        lines.append(f"{label}:\n{transcript[:3500]}")

    if content_type == "Video":
        if visual_summary or transcript:
            lines.append(
                "Use the visible video content first. Use the spoken transcript only if it is "
                "clearly relevant. Ask a focused follow-up question about the visible concern; "
                "do not say the message was unclear when visible content is available."
            )
        else:
            has_visible_frames = video_has_visible_frames(media_url)
            if has_visible_frames:
                lines.append("The video file has visible frames, but no local vision-description model is active.")
            lines.append(
                "The video was received successfully, but no usable voice transcript could be "
                "generated. Do not say 'isme clearly kuch samajh nahi aa raha' and do not ask "
                "the customer to resend the same video as the first response. Reply in Hindi/Hinglish: "
                "video mil gaya hai, isme voice/text clear nahi hai, doctor/review team ko forward "
                "kar rahe hain, and ask for patient name, age, symptoms, and a clear photo if available."
            )
    elif transcript:
        lines.append(
            "Use the transcript as the customer's latest message. Reply to what they asked, "
            "and mention only if any part was unclear."
        )
    else:
        lines.append(
            f"{content_type} could not be transcribed. Acknowledge receipt and ask the customer "
            "to resend it or type the details."
        )
    return "\n".join(lines)


def process_transcript_for_lead_summary(
    conversation: str,
    message_name: str,
    payload: Dict,
) -> None:
    convo = frappe.get_doc("Chat Conversation", conversation)
    crm_lead = get_conversation_crm_lead(convo)
    if not crm_lead:
        return

    media_url = payload.get("media_url")
    if not media_url:
        media_url = frappe.db.get_value("Chat Message", message_name, "media_url")
    if not media_url:
        return

    content_type = str(payload.get("content_type") or "Audio").title()
    if content_type not in TRANSCRIPT_CONTENT_TYPES:
        return

    body_hint = str(payload.get("body") or "").strip()
    transcript = transcribe_media(media_url, content_type)
    summary = summarize_transcript(transcript, body_hint, content_type)
    note_block = _build_transcript_notes_block(
        content_type=content_type,
        message_name=message_name,
        media_url=media_url,
        body_hint=body_hint,
        summary=summary,
    )
    _append_to_lead_notes("CRM Lead", crm_lead, note_block)


def transcribe_media(media_url: str, content_type: str = "Audio") -> str:
    content_type = str(content_type or "Audio").title()
    if content_type not in TRANSCRIPT_CONTENT_TYPES:
        return ""

    media = _download_media(media_url, content_type)
    if not media:
        return ""

    provider = _get_audio_transcription_provider()
    if not provider:
        return ""

    upload = _prepare_transcription_upload(media_url, media, content_type)
    media_bytes = upload.get("content") or b""
    if not media_bytes:
        return ""

    filename = upload.get("filename")
    model = _audio_transcription_model(provider)
    upload_mime = upload.get("mime_type")

    endpoint = _build_audio_transcription_url(provider.get("base_url"))

    try:
        transcript = _post_transcription(
            endpoint=endpoint,
            provider=provider,
            model=model,
            filename=filename,
            media_bytes=media_bytes,
            mime_type=upload_mime,
            content_type=content_type,
        )
        return transcript if _is_useful_transcript(transcript, content_type) else ""
    except Exception:
        if content_type == "Video":
            for fallback_mime in _video_transcription_fallback_mimes(upload_mime):
                try:
                    transcript = _post_transcription(
                        endpoint=endpoint,
                        provider=provider,
                        model=model,
                        filename=filename,
                        media_bytes=media_bytes,
                        mime_type=fallback_mime,
                        content_type=content_type,
                    )
                    return transcript if _is_useful_transcript(transcript, content_type) else ""
                except Exception:
                    continue
        frappe.log_error(
            frappe.get_traceback()
            + "\n\n"
            + frappe.as_json(
                {
                    "content_type": content_type,
                    "source_mime_type": media.get("mime_type"),
                    "upload_mime_type": upload_mime,
                    "filename": filename,
                    "size_bytes": len(media_bytes),
                    "endpoint": endpoint,
                    "model": model,
                }
            ),
            f"WA {content_type} Transcription Failed",
        )
        return ""


def describe_video_media(media_url: str) -> str:
    local_summary = describe_video_frames_locally(media_url)

    providers = _get_vision_providers()
    if not providers:
        return local_summary

    media = _download_media(media_url, "Video")
    if not media or not (media.get("content") or b""):
        return local_summary

    frames = _extract_video_frames(media.get("content") or b"")
    if not frames:
        return local_summary

    content = [
        {
            "type": "text",
            "text": (
                "These are sampled frames from a WhatsApp video sent by a customer. "
                "Describe the visible content objectively in 1-3 short sentences. "
                "If this appears related to a healthcare concern, mention what visible details "
                "could be relevant, but do not diagnose or prescribe."
            ),
        }
    ]
    for frame in frames[:4]:
        encoded = base64.b64encode(frame).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )

    for provider in providers:
        base_url = provider["base_url"] or "https://api.openai.com/v1/chat/completions"
        if base_url.endswith("/") and "chat/completions" not in base_url:
            base_url = f"{base_url}chat/completions"

        try:
            resp = requests.post(
                base_url,
                headers={
                    "Authorization": f"Bearer {provider['api_key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": provider["model_name"],
                    "messages": [{"role": "user", "content": content}],
                    "temperature": 0,
                    "max_tokens": 600,
                },
                timeout=45,
            )
            resp.raise_for_status()
            summary = resp.json()["choices"][0]["message"].get("content", "")
            if summary:
                return summary[:4000]
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"WA Video Vision Summary Failed ({provider.get('name') or provider.get('model_name')})",
            )
    return local_summary


def describe_video_frames_locally(media_url: str) -> str:
    """Cheap visual triage for videos when no vision-language model is active."""
    media = _download_media(media_url, "Video")
    if not media or not (media.get("content") or b""):
        return ""

    frames = _extract_video_frames(media.get("content") or b"")
    if not frames:
        return ""

    observations = [_analyze_video_frame(frame) for frame in frames]
    observations = [obs for obs in observations if obs]
    if not observations:
        return ""

    skin_frames = [obs for obs in observations if obs.get("skin_ratio", 0) >= 0.08]
    if not skin_frames:
        return "Sampled frames are visible, but no clear skin/body area was detected locally."

    avg_skin = sum(obs.get("skin_ratio", 0) for obs in skin_frames) / len(skin_frames)
    avg_red = sum(obs.get("red_ratio", 0) for obs in skin_frames) / len(skin_frames)
    avg_texture = sum(obs.get("texture_ratio", 0) for obs in skin_frames) / len(skin_frames)
    avg_spot = sum(obs.get("spot_ratio", 0) for obs in skin_frames) / len(skin_frames)

    details = ["Sampled frames show a close-up of a visible skin/body area."]
    if avg_red >= 0.08:
        details.append("There appears to be visible redness/irritation in part of the area.")
    elif avg_red >= 0.035:
        details.append("There may be mild redness/irritation.")

    if avg_texture >= 0.055:
        details.append("The skin surface looks uneven/rough, which can be seen with dryness, scaling, rash, or irritation.")
    elif avg_texture >= 0.03:
        details.append("There is some visible texture/roughness on the skin surface.")

    if avg_spot >= 0.02:
        details.append("Small darker or patchy spots are visible in the sampled frames.")

    if len(details) == 1 and avg_skin >= 0.25:
        details.append("No obvious printed report/text is visible; treat this as a visual skin/body concern.")

    details.append(
        "This is not a diagnosis. Ask about itching, pain/burning, swelling, discharge, fever, duration, and whether it is spreading; advise doctor/dermatology review if severe or worsening."
    )
    return " ".join(details)[:2000]


def _analyze_video_frame(frame_bytes: bytes) -> dict:
    if not frame_bytes:
        return {}
    try:
        import cv2
        import numpy as np

        data = np.frombuffer(frame_bytes, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if image is None:
            return {}

        h, w = image.shape[:2]
        if h <= 0 or w <= 0:
            return {}

        ycrcb = cv2.cvtColor(image, cv2.COLOR_BGR2YCrCb)
        lower = np.array([0, 133, 77], dtype=np.uint8)
        upper = np.array([255, 173, 127], dtype=np.uint8)
        skin_mask = cv2.inRange(ycrcb, lower, upper) > 0

        skin_count = int(np.count_nonzero(skin_mask))
        total = int(h * w)
        if not total:
            return {}

        b, g, r = cv2.split(image)
        red_mask = skin_mask & (r > 120) & (r > (g.astype("float32") * 1.12)) & (r > (b.astype("float32") * 1.18))
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 60, 140) > 0
        texture_mask = skin_mask & edges
        spot_mask = skin_mask & (gray < 95)

        return {
            "skin_ratio": skin_count / total,
            "red_ratio": int(np.count_nonzero(red_mask)) / max(skin_count, 1),
            "texture_ratio": int(np.count_nonzero(texture_mask)) / max(skin_count, 1),
            "spot_ratio": int(np.count_nonzero(spot_mask)) / max(skin_count, 1),
        }
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Local Video Frame Analysis Failed")
        return {}


def video_has_visible_frames(media_url: str) -> bool:
    media = _download_media(media_url, "Video")
    if not media or not (media.get("content") or b""):
        return False
    return bool(_extract_video_frames(media.get("content") or b""))


def _prepare_transcription_upload(media_url: str, media: Dict, content_type: str) -> Dict:
    media_bytes = media.get("content") or b""
    source_mime = media.get("mime_type")
    filename = _guess_transcription_filename(media_url, source_mime, content_type)
    upload_mime = _transcription_upload_mime(source_mime, filename, content_type)

    if content_type != "Video":
        converted = _convert_audio_to_mp3(media_bytes, filename)
        if converted:
            return {
                "content": converted,
                "filename": f"wa-audio-{frappe.generate_hash(length=8)}.mp3",
                "mime_type": "audio/mpeg",
            }
        return {"content": media_bytes, "filename": filename, "mime_type": upload_mime}

    extracted = _extract_audio_from_video(media_bytes)
    if extracted:
        return {
            "content": extracted,
            "filename": f"wa-video-audio-{frappe.generate_hash(length=8)}.mp3",
            "mime_type": "audio/mpeg",
        }

    return {"content": media_bytes, "filename": filename, "mime_type": upload_mime}


def _convert_audio_to_mp3(audio_bytes: bytes, filename: str | None = None) -> bytes:
    if not audio_bytes:
        return b""
    ffmpeg = _get_ffmpeg_executable()
    if not ffmpeg:
        frappe.log_error(
            "ffmpeg is not available. Audio will be sent to transcription provider in original format.",
            "WA Audio Conversion Missing",
        )
        return b""

    suffix = os.path.splitext(str(filename or ""))[1] or ".ogg"
    input_path = None
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as source:
            source.write(audio_bytes)
            input_path = source.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as target:
            output_path = target.name

        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                input_path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "64k",
                output_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=True,
        )
        with open(output_path, "rb") as audio_file:
            return audio_file.read()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Audio Conversion Failed")
        return b""
    finally:
        for path in (input_path, output_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


def _extract_audio_from_video(video_bytes: bytes) -> bytes:
    ffmpeg = _get_ffmpeg_executable()
    if not ffmpeg:
        frappe.log_error(
            "ffmpeg is not available. Install imageio-ffmpeg in the bench env or install system ffmpeg to transcribe video audio tracks reliably.",
            "WA Video Audio Extraction Missing",
        )
        return b""

    input_path = None
    output_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as source:
            source.write(video_bytes)
            input_path = source.name
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as target:
            output_path = target.name

        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                input_path,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                "64k",
                output_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=True,
        )
        with open(output_path, "rb") as audio_file:
            return audio_file.read()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Video Audio Extraction Failed")
        return b""
    finally:
        for path in (input_path, output_path):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except Exception:
                    pass


def _get_ffmpeg_executable() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def _extract_video_frames(video_bytes: bytes) -> list[bytes]:
    ffmpeg = _get_ffmpeg_executable()
    if not ffmpeg:
        frappe.log_error(
            "ffmpeg is not available. Install imageio-ffmpeg in the bench env or install system ffmpeg to summarize video frames.",
            "WA Video Frame Extraction Missing",
        )
        return []

    input_path = None
    output_dir = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as source:
            source.write(video_bytes)
            input_path = source.name
        output_dir = tempfile.mkdtemp(prefix="wa-video-frames-")
        frame_pattern = os.path.join(output_dir, "frame-%02d.jpg")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                input_path,
                "-vf",
                "fps=1/4,scale='min(768,iw)':-2",
                "-frames:v",
                "4",
                "-q:v",
                "3",
                frame_pattern,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=True,
        )
        frames = []
        for filename in sorted(os.listdir(output_dir)):
            if filename.lower().endswith(".jpg"):
                with open(os.path.join(output_dir, filename), "rb") as frame_file:
                    frames.append(frame_file.read())
        return frames
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Video Frame Extraction Failed")
        return []
    finally:
        if input_path and os.path.exists(input_path):
            try:
                os.remove(input_path)
            except Exception:
                pass
        if output_dir and os.path.isdir(output_dir):
            try:
                shutil.rmtree(output_dir)
            except Exception:
                pass


def _post_transcription(
    *,
    endpoint: str,
    provider: Dict,
    model: str,
    filename: str,
    media_bytes: bytes,
    mime_type: str,
    content_type: str,
) -> str:
    resp = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {provider['api_key']}",
        },
        data={
            "model": model,
            "temperature": 0,
        },
        files={
            "file": (filename, media_bytes, mime_type or "application/octet-stream"),
        },
        timeout=90,
    )
    if resp.status_code >= 400:
        frappe.log_error(
            frappe.as_json(
                {
                    "status_code": resp.status_code,
                    "response": (resp.text or "")[:2000],
                    "content_type": content_type,
                    "upload_mime_type": mime_type,
                    "filename": filename,
                    "size_bytes": len(media_bytes),
                }
            ),
            f"WA {content_type} Transcription API Failure",
        )
    resp.raise_for_status()
    data = resp.json()
    return str(data.get("text") or data.get("transcript") or "")[:12000]



def _audio_format(filename: str | None, mime_type: str | None) -> str:
    extension = str(filename or "").rsplit(".", 1)[-1].lower() if "." in str(filename or "") else ""
    if extension in {"wav", "mp3", "flac", "m4a", "ogg", "webm", "aac"}:
        return extension
    mime = str(mime_type or "").split(";", 1)[0].strip().lower()
    return {
        "audio/wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/flac": "flac",
        "audio/mp4": "m4a",
        "audio/ogg": "ogg",
        "audio/webm": "webm",
        "audio/aac": "aac",
    }.get(mime, "mp3")


def _is_useful_transcript(transcript: str, content_type: str) -> bool:
    text = str(transcript or "").strip()
    if not text:
        return False
    if content_type != "Video":
        return True
    if _looks_like_unrelated_video_transcript(text):
        return False
    words = [word for word in text.replace("\n", " ").split(" ") if word.strip()]
    generic = {"you", "yeah", "yes", "no", "ok", "okay", "hmm", "um", "uh"}
    if len(words) <= 2 and text.lower().strip(" .,!?:;") in generic:
        return False
    return len(words) >= 3 or len(text) >= 18


def _looks_like_unrelated_video_transcript(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    hangul_or_cjk = [
        ch
        for ch in letters
        if ("\uac00" <= ch <= "\ud7af")
        or ("\u3040" <= ch <= "\u30ff")
        or ("\u4e00" <= ch <= "\u9fff")
    ]
    return len(hangul_or_cjk) / max(len(letters), 1) > 0.35


def summarize_transcript(transcript: str, body_hint: str, content_type: str) -> str:
    transcript = (transcript or "").strip()
    content_type = str(content_type or "Audio").title()
    if transcript:
        provider = _get_openai_compatible_provider()
        if provider:
            summary = _summarize_transcript_with_model(provider, transcript, content_type)
            if summary:
                return summary
        return (
            f"{content_type} transcript:\n"
            f"- {transcript[:1200]}"
        )
    if body_hint:
        return f"No {content_type.lower()} transcript generated. User caption/body: {body_hint}"
    return f"No {content_type.lower()} transcript generated."


def _build_transcript_notes_block(
    content_type: str,
    message_name: str,
    media_url: str,
    body_hint: str,
    summary: str,
) -> str:
    stamp = datetime.now().strftime("%d-%b-%Y %H:%M")
    caption = _clean_caption(body_hint)
    lines = [
        f"[WA-TRANSCRIPT | {stamp} | {content_type}]",
        f"Ref: Chat Message {message_name}",
    ]
    if caption:
        lines.append(f"Caption: {caption}")
    lines.extend(
        [
            TRANSCRIPT_NOTE_SEPARATOR,
            _normalize_summary_sections(summary.strip(), content_type),
            TRANSCRIPT_NOTE_SEPARATOR,
            f"Source: {media_url}",
        ]
    )
    return "\n".join(lines)


def _normalize_summary_sections(summary: str, content_type: str) -> str:
    if not summary:
        return (
            "Report summary:\n"
            f"- No {content_type.lower()} transcript generated.\n\n"
            "Suggested follow-up:\n"
            "- Ask customer to resend the media or type the details."
        )

    required_headers = (
        "Report summary:",
        "Key findings:",
        "Abnormal values:",
        "Suggested follow-up:",
    )
    lowered = summary.lower()
    if any(header.lower() in lowered for header in required_headers):
        return summary

    return (
        "Report summary:\n"
        f"- {summary.replace(chr(10), chr(10) + '- ')}\n\n"
        "Key findings:\n"
        "- See transcript summary above.\n\n"
        "Abnormal values:\n"
        "- None noted unless the customer mentioned medical values explicitly.\n\n"
        "Suggested follow-up:\n"
        "- Review transcript and confirm details with the customer."
    )


def _resolve_notes_fieldname(doctype: str) -> Optional[str]:
    meta = frappe.get_meta(doctype)
    if meta.has_field("sr_lead_notes"):
        return "sr_lead_notes"
    if meta.has_field("notes"):
        return "notes"
    return None


def _append_to_lead_notes(doctype: str, name: str, note_block: str) -> None:
    lead_doc = frappe.get_doc(doctype, name)
    notes_field = _resolve_notes_fieldname(doctype)
    if notes_field:
        existing = str(getattr(lead_doc, notes_field, "") or "").strip()
        merged = f"{existing}\n\n{note_block}".strip() if existing else note_block
        if len(merged) > SR_LEAD_NOTES_MAX_LEN:
            merged = _trim_notes_to_limit(existing, note_block, SR_LEAD_NOTES_MAX_LEN)
        setattr(lead_doc, notes_field, merged)
        lead_doc.save(ignore_permissions=True)
    lead_doc.add_comment("Comment", note_block)


def _trim_notes_to_limit(existing: str, new_block: str, max_len: int) -> str:
    if len(new_block) >= max_len:
        return new_block[: max_len - 20] + "\n... [truncated]"
    budget = max_len - len(new_block) - 2
    if not existing:
        return new_block
    if len(existing) <= budget:
        return f"{existing}\n\n{new_block}".strip()
    trimmed_existing = existing[-budget:]
    if "\n\n" in trimmed_existing:
        trimmed_existing = trimmed_existing.split("\n\n", 1)[-1]
    return f"... [older notes truncated]\n\n{trimmed_existing}\n\n{new_block}".strip()


def _download_media(media_url: str, content_type: str) -> Optional[Dict]:
    try:
        resp = requests.get(
            media_url,
            headers={
                "Accept": "*/*",
                "User-Agent": "wa-chat-hub/1.0",
            },
            timeout=45,
        )
        resp.raise_for_status()
        content = resp.content or b""
        if not content:
            return None
        mime_type = str(resp.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if not mime_type:
            mime_type = mimetypes.guess_type(str(media_url).split("?", 1)[0])[0] or _default_mime_type(content_type)
        return {"content": content, "mime_type": mime_type}
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"WA {content_type} Download Failed")
        return None


def _get_audio_transcription_provider() -> Optional[Dict]:
    rows = get_active_llm_provider_rows(TRANSCRIPTION_CAPABILITY, limit=5)
    for row in rows:
        provider = get_provider_secret(row)
        if provider:
            return provider
    frappe.log_error(
        "No active WA LLM Provider found for media transcription. Configure a provider with Use for Audio / Video Transcription enabled and /audio/transcriptions support.",
        "WA Media Transcription Provider Missing",
    )
    return None


def _get_openai_compatible_provider() -> Optional[Dict]:
    rows = get_active_llm_provider_rows(CHAT_CAPABILITY, limit=5)
    for row in rows:
        provider = get_provider_secret(row)
        if provider:
            return provider
    return None


def _get_vision_providers() -> list[Dict]:
    rows = get_active_llm_provider_rows(VISION_CAPABILITY, limit=5)
    providers = []
    for row in rows:
        provider = get_provider_secret(row)
        if provider:
            providers.append(provider)
    if not providers:
        frappe.log_error(
            "No active vision-capable WA LLM Provider found for video frame summaries. Configure a provider with Use for Vision / OCR enabled.",
            "WA Video Vision Provider Missing",
        )
    return providers


def _get_vision_provider() -> Optional[Dict]:
    providers = _get_vision_providers()
    return providers[0] if providers else None


def _looks_like_vision_model(provider_type: str, model_name: str | None) -> bool:
    return looks_like_vision_model(provider_type, model_name)


def _build_audio_transcription_url(base_url: str | None) -> str:
    base_url = (base_url or "").strip() or "https://api.openai.com/v1"
    if base_url.endswith("/chat/completions"):
        base_url = base_url[: -len("/chat/completions")]
    if base_url.endswith("/responses"):
        base_url = base_url[: -len("/responses")]
    base_url = base_url.rstrip("/")
    if base_url.endswith("/audio/transcriptions"):
        return base_url
    return f"{base_url}/audio/transcriptions"


def _audio_transcription_model(provider: Dict) -> str:
    configured = str(provider.get("model_name") or "").strip()
    model_name = configured.lower()
    if "whisper" in model_name or "transcribe" in model_name:
        return configured
    return "whisper-1"


def _guess_transcription_filename(media_url: str, mime_type: str | None, content_type: str) -> str:
    path_name = str(media_url or "").split("?", 1)[0].rstrip("/").split("/")[-1]
    if "." in path_name:
        return path_name[:140]
    ext = mimetypes.guess_extension(str(mime_type or "").split(";", 1)[0].strip())
    if not ext:
        ext = ".mp4" if content_type == "Video" else ".ogg"
    prefix = "wa-video" if content_type == "Video" else "wa-audio"
    return f"{prefix}-{frappe.generate_hash(length=8)}{ext}"


def _transcription_upload_mime(mime_type: str | None, filename: str, content_type: str) -> str:
    normalized = str(mime_type or "").split(";", 1)[0].strip().lower()
    extension = str(filename or "").rsplit(".", 1)[-1].lower() if "." in str(filename or "") else ""
    if content_type == "Video":
        if extension in {"mp4", "m4a", "m4v"}:
            return "audio/mp4"
        if extension == "webm":
            return "audio/webm"
        if extension == "ogg":
            return "audio/ogg"
        if normalized == "video/mp4":
            return "audio/mp4"
        if normalized == "video/webm":
            return "audio/webm"
    return normalized or "application/octet-stream"


def _video_transcription_fallback_mimes(primary_mime: str) -> list[str]:
    fallbacks = ["audio/mp4", "video/mp4", "application/octet-stream"]
    return [mime for mime in fallbacks if mime != primary_mime]


def _default_mime_type(content_type: str) -> str:
    return "video/mp4" if str(content_type or "").title() == "Video" else "audio/ogg"


def _clean_caption(body_hint: str) -> str:
    text = str(body_hint or "").strip()
    if text.lower() in {
        "",
        "none",
        "null",
        "undefined",
        "audio",
        "video",
        "voice",
        "voice note",
        "audio message received",
        "video message received",
        "voice message received",
        "[audio message received]",
        "[video message received]",
        "[voice message received]",
    }:
        return ""
    return text


def _summarize_transcript_with_model(provider: Dict, transcript: str, content_type: str) -> str:
    base_url = provider["base_url"] or "https://api.openai.com/v1/chat/completions"
    if base_url.endswith("/") and "chat/completions" not in base_url:
        base_url = f"{base_url}chat/completions"
    prompt = (
        f"Summarize this WhatsApp {content_type.lower()} transcript for CRM lead notes. "
        "Return ONLY plain text using exactly these section headers and bullet lines:\n"
        "Report summary:\n"
        "- <1-3 short bullets about what the customer said>\n\n"
        "Key findings:\n"
        "- <important symptoms, requests, appointment/payment/order details, or 'None noted'>\n\n"
        "Abnormal values:\n"
        "- None noted unless the customer mentioned medical values explicitly\n\n"
        "Suggested follow-up:\n"
        "- <safe operational follow-up questions, no diagnosis or prescriptions>\n\n"
        f"Transcript:\n{transcript[:10000]}"
    )
    try:
        resp = requests.post(
            base_url,
            headers={
                "Authorization": f"Bearer {provider['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": provider["model_name"],
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"].get("content", "")[:4000]
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"WA {content_type} Summary Generation Failed")
        return ""
