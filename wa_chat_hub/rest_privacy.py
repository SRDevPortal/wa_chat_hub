"""Mask native v1/v2 Conversation read responses after framework authorization."""
import json

import frappe
from werkzeug.exceptions import HTTPException

from wa_chat_hub.client_privacy import project_read
from wa_chat_hub.number_privacy import restricted


def protect_conversation_read(request, response):
    from frappe.api import API_URL_MAP
    from frappe.api import v1, v2

    if request.method != "GET" or not 200 <= response.status_code < 300:
        return
    try:
        endpoint, arguments = API_URL_MAP.bind_to_environ(request.environ).match()
    except HTTPException:
        return
    if (endpoint not in (v1.document_list, v1.read_doc, v2.document_list, v2.read_doc, v2.copy_doc)
            or arguments.get("doctype") != "Chat Conversation"
            or "run_method" in frappe.form_dict):
        return
    # After-request exceptions are swallowed by Frappe. Never return the raw
    # successful document if the policy lookup or projection fails.
    try:
        if not restricted():
            return
        data = response.get_json()
        data["data"] = project_read(data["data"], "Chat Conversation")
        response.set_data(json.dumps(data, ensure_ascii=False, default=str))
        response.headers["Cache-Control"] = "no-store"
    except Exception:
        response.status_code = 500
        response.set_data(json.dumps({"exc_type": "PrivacyProjectionError",
                                     "message": "Unable to prepare a protected conversation response."}))
        response.headers["Cache-Control"] = "no-store"
    response.mimetype = "application/json"
