"""Private authentication transport. Never creates chat records or logs payloads.

No guest/whitelisted sending API: Login Security chooses the enrolled recipient
and approved template after validating the authentication transaction.
"""

import re
from urllib.parse import urlsplit

import frappe
import phonenumbers
import requests


def validate_account(name):
    account = frappe.get_doc("Chat Channel Account", name)
    url = account.interakt_base_url or "https://api.interakt.ai/v1/public/message/"
    parsed = urlsplit(url)
    if (
        not account.is_active
        or account.channel_type != "Interakt"
        or parsed.scheme != "https"
        or parsed.netloc != "api.interakt.ai"
        or parsed.path.rstrip("/") != "/v1/public/message"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "An active Interakt account with its official HTTPS endpoint is required."
        )
    if not account.get_password("interakt_api_key", raise_exception=False):
        raise ValueError("The authentication sender is not configured.")
    return account


def send_login_otp(account_name, phone, code, template, language, reference):
    account = validate_account(account_name)
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError("Invalid verification code format.")
    parsed = phonenumbers.parse(phone, None)
    if not phone.startswith("+") or not phonenumbers.is_valid_number(parsed):
        raise ValueError("Invalid verification destination.")
    payload = {
        "countryCode": f"+{parsed.country_code}",
        "phoneNumber": phonenumbers.national_significant_number(parsed),
        "callbackData": "login_security:" + reference,
        "type": "Template",
        "template": {
            "name": template,
            "languageCode": language,
            "bodyValues": [code],
            "buttonValues": {"0": [code]},
        },
    }
    try:
        response = requests.post(
            "https://api.interakt.ai/v1/public/message/",
            headers={
                "Authorization": "Basic " + account.get_password("interakt_api_key"),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=(3, 8),
            allow_redirects=False,
        )
        if response.status_code >= 500 or response.status_code in (408, 429):
            return {"outcome": "unknown"}
        if not 200 <= response.status_code < 300:
            return {"outcome": "rejected"}
        result = response.json()
        if not isinstance(result, dict):
            return {"outcome": "unknown"}
        if result.get("result") is False:
            return {"outcome": "rejected"}
        if result.get("result") is not True and not result.get("id"):
            return {"outcome": "unknown"}
        message_id = result.get("id")
        if not isinstance(message_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,150}", message_id
        ):
            message_id = None
        return {"outcome": "accepted", "message_id": message_id}
    except (requests.RequestException, ValueError):
        # Exception strings and response bodies may contain the code or API key.
        return {"outcome": "unknown"}
