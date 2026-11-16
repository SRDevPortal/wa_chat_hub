"""Opt-in queue isolation. Provision consumers before configuring these keys."""
import re

import frappe


def ai_queue():
    value = (getattr(frappe.local, "conf", None) or {}).get("wa_ai_queue") or "short"
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("wa_ai_queue must be a valid provisioned queue name")
    return value
