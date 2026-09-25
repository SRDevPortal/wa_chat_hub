"""Account identity for opted-in Mobile App channels only.

These contacts deliberately have no routable phone. Profile membership scopes a
chat; it does not, by itself, prove ownership of a medical Patient record.
"""
import hashlib
import json

import frappe
from frappe import _
from frappe.utils import cint


def enabled(channel_account):
    if not cint(frappe.conf.get("mobile_app_ai_account_identity")):
        return False
    account = frappe.get_cached_doc("Chat Channel Account", channel_account)
    return account.channel_type == "Mobile App"


def profile_key(user, requested=None):
    rows = list(user.get("profiles") or [])
    requested = str(requested or "").strip()
    if requested:
        # Stable child-row IDs only: names and Patient IDs are not identifiers
        # of a unique profile and may be supplied by a user during profile sync.
        rows = [row for row in rows if str(row.name) == requested]
        if not rows:
            frappe.throw(_("The selected profile does not belong to this user."), frappe.PermissionError)
    if len(rows) > 1:
        frappe.throw(_("Select a patient profile before starting AI chat."))
    return str(rows[0].name) if rows else ""


def identity_key(account, user, profile):
    raw = json.dumps([str(account), str(user), str(profile)], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def ensure_contact(context):
    from wa_chat_hub.services import safe_ai_insert, safe_ai_get_value

    key = context["mobile_account_key"]
    existing = safe_ai_get_value("Chat Contact", {"mobile_account_key": key}, "name")
    if existing:
        return existing
    doc = frappe.get_doc({
        "doctype": "Chat Contact", "name": "mobile-" + key,
        "mobile_account_key": key, "mobile_channel_account": context["account"],
        "mobile_app_user": str(context["user"].name),
        "mobile_profile_id": context["profile_key"],
        "display_name": context["user"].full_name or "App user",
        "phone_number": None,
    })
    # Serialize session creation on the existing user row, before looking up
    # the contact again. The caller commits the session before releasing it.
    frappe.db.get_value("Mobile App User", context["user"].name, "name", for_update=True)
    existing = safe_ai_get_value("Chat Contact", {"mobile_account_key": key}, "name", for_update=True)
    if existing:
        return existing
    safe_ai_insert(doc)
    return doc.name
