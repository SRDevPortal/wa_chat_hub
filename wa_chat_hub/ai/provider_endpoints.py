"""Normalize base and legacy full URLs for the supported API operations."""
from urllib.parse import urlsplit, urlunsplit


def operation_url(base_url, operation="chat"):
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provider Base URL must be an HTTP(S) URL without credentials, query or fragment.")
    path = parsed.path.rstrip("/")
    for suffix in ("/chat/completions", "/audio/transcriptions", "/responses"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    suffix = "/audio/transcriptions" if operation == "transcription" else "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path + suffix, "", ""))
