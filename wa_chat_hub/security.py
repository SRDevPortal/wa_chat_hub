from __future__ import annotations

import json

import frappe
from frappe import _


DOCTYPE_PERMISSION_ACTIONS = {
    "read": "allow_read",
    "write": "allow_write",
    "delete": "allow_delete",
}


DEFAULT_AI_DOCTYPE_PERMISSIONS = (
    ("DocType", 1, 0, 0, "Read DocType metadata/existence for WA Chat Hub service decisions."),
    ("WA Chat Hub Settings", 1, 0, 0, "Read settings needed by WA Chat Hub AI runtime."),
    ("Chat Channel Account", 1, 0, 0, "Read channel/account configuration."),
    ("Chat Contact", 1, 1, 0, "Create and update WhatsApp contacts."),
    ("Chat Contact Channel Profile", 1, 1, 0, "Maintain per-channel contact profile mapping."),
    ("Chat Conversation", 1, 1, 0, "Create and update WhatsApp conversations."),
    ("Chat Message", 1, 1, 0, "Create and update WhatsApp messages."),
    ("Chat Assignment Rule", 1, 0, 0, "Read active assignment rules for inbound chat routing."),
    ("Chat Channel Session", 1, 1, 0, "Maintain provider/session state."),
    ("WA LLM Provider", 1, 0, 0, "Read LLM provider configuration."),
    ("WA AI Knowledge Base", 1, 0, 0, "Read AI knowledge snippets."),
    ("WA Channel Context", 1, 0, 0, "Read channel context configuration."),
    ("WA Channel Context Knowledge Base", 1, 0, 0, "Read linked knowledge base rows."),
    ("WA Channel Account Prompt Map", 1, 0, 0, "Read per-account prompt overrides."),
    ("WA Channel Pipeline Map", 1, 0, 0, "Read channel to pipeline/source routing configuration."),
    ("WA MCP Server", 1, 0, 0, "Read enabled MCP server definitions for AI function calling."),
    ("WA MCP Tool Endpoint", 1, 0, 0, "Read enabled MCP tool definitions for AI function calling."),
    ("Chat AI Suggestion", 1, 1, 0, "Create AI draft suggestions."),
    ("Chat Action Log", 1, 1, 0, "Write chat action audit logs."),
    ("WA Lead OCR Result", 1, 1, 0, "Store OCR extraction output."),
    ("WA Lead AI Insight", 1, 1, 0, "Store AI lead scoring/extraction insight output."),
    ("File", 1, 1, 0, "Read/write media attachments managed by WA Chat Hub."),
    ("Customer", 1, 0, 0, "Read customer phone/name for WhatsApp contact sync."),
    ("Lead", 1, 0, 0, "Read lead phone/name for WhatsApp contact sync."),
    ("CRM Lead", 1, 1, 0, "Read/create/update CRM leads linked to WhatsApp conversations."),
    ("CRM Lead Status", 1, 0, 0, "Read default CRM lead status for inbound lead creation."),
    ("CRM Lead Source", 1, 1, 0, "Resolve/create CRM lead source values such as WhatsApp."),
    ("Lead Source", 1, 1, 0, "Resolve/create fallback Lead source values such as WhatsApp."),
    ("SR Lead Source", 1, 1, 0, "Resolve/create SR lead source values mapped from WhatsApp channels."),
    ("SR Lead Pipeline", 1, 0, 0, "Read pipeline links for inbound CRM lead routing."),
    ("SR Lead Platform", 1, 1, 0, "Resolve/create SR lead platform values such as WhatsApp."),
    ("Contact", 1, 0, 0, "Read linked contact details for WhatsApp context."),
    ("Address", 1, 0, 0, "Read linked address details for WhatsApp context."),
    ("Comment", 1, 1, 0, "Repair attachment comments created for WhatsApp media files."),
    ("Department", 1, 0, 0, "Validate routed chat department links."),
    ("User", 1, 0, 0, "Validate assignment/service user links."),
)


class WAChatHubSecurityError(frappe.PermissionError):
    pass


def get_service_user() -> str:
    if not frappe.db.exists("DocType", "WA Chat Hub Settings"):
        raise WAChatHubSecurityError(_("WA Chat Hub Settings DocType is not installed."))
    settings = frappe.get_single("WA Chat Hub Settings")
    user = (getattr(settings, "service_user", None) or "").strip()
    if not user:
        raise WAChatHubSecurityError(_("WA Chat Hub Service User is not configured."))
    if user in {"Administrator", "Guest"}:
        raise WAChatHubSecurityError(_("WA Chat Hub Service User cannot be Administrator or Guest."))
    if not frappe.db.exists("User", user):
        raise WAChatHubSecurityError(_("WA Chat Hub Service User {0} does not exist.").format(user))
    enabled = frappe.db.get_value("User", user, "enabled")
    if not enabled:
        raise WAChatHubSecurityError(_("WA Chat Hub Service User {0} is disabled.").format(user))
    return user


def set_service_user_context(operation: str | None = None) -> str:
    user = get_service_user()
    frappe.set_user(user)
    set_ai_security_context(operation=operation)
    return user


def set_ai_security_context(
    *,
    operation: str | None = None,
    conversation: str | None = None,
    message: str | None = None,
    channel_account: str | None = None,
) -> None:
    if operation is not None:
        frappe.flags.wa_chat_hub_service_operation = operation or ""
    if conversation is not None:
        frappe.flags.wa_chat_hub_security_conversation = conversation or ""
    if message is not None:
        frappe.flags.wa_chat_hub_security_message = message or ""
    if channel_account is not None:
        frappe.flags.wa_chat_hub_security_channel_account = channel_account or ""


def has_ai_doctype_permission(doctype: str, action: str) -> bool:
    fieldname = DOCTYPE_PERMISSION_ACTIONS.get((action or "").strip().lower())
    doctype = (doctype or "").strip()
    if not doctype or not fieldname:
        return False
    if not frappe.db.exists("DocType", "WA Chat Hub Settings"):
        return False
    settings = frappe.get_single("WA Chat Hub Settings")
    for row in settings.get("ai_doctype_permissions") or []:
        if not row.get("is_active"):
            continue
        if (row.get("doctype_name") or "").strip() != doctype:
            continue
        return bool(row.get(fieldname))
    return False


def assert_ai_doctype_permission(doctype: str, action: str) -> None:
    if has_ai_doctype_permission(doctype, action):
        return
    _log_blocked_access(doctype, action)
    raise WAChatHubSecurityError(
        _("WA Chat Hub AI is not permitted to {0} DocType {1}.").format(action, doctype)
    )


def safe_ai_get_doc(doctype: str, name: str):
    assert_ai_doctype_permission(doctype, "read")
    return frappe.get_doc(doctype, name)


def safe_ai_get_all(doctype: str, *args, **kwargs):
    assert_ai_doctype_permission(doctype, "read")
    return frappe.get_all(doctype, *args, **kwargs)


def safe_ai_get_value(doctype: str, filters=None, fieldname=None, *args, **kwargs):
    assert_ai_doctype_permission(doctype, "read")
    return frappe.db.get_value(doctype, filters, fieldname, *args, **kwargs)


def safe_ai_set_value(doctype: str, name: str, fieldname, value=None, *args, **kwargs):
    assert_ai_doctype_permission(doctype, "write")
    return frappe.db.set_value(doctype, name, fieldname, value, *args, **kwargs)


def safe_ai_exists(doctype: str, name_or_filters=None, *args, **kwargs):
    assert_ai_doctype_permission(doctype, "read")
    return frappe.db.exists(doctype, name_or_filters, *args, **kwargs)


def safe_ai_insert(doc, **kwargs):
    assert_ai_doctype_permission(doc.doctype, "write")
    kwargs.setdefault("ignore_permissions", True)
    return doc.insert(**kwargs)


def safe_ai_save(doc, **kwargs):
    assert_ai_doctype_permission(doc.doctype, "write")
    kwargs.setdefault("ignore_permissions", True)
    return doc.save(**kwargs)


def safe_ai_delete_doc(doctype: str, name: str, **kwargs):
    assert_ai_doctype_permission(doctype, "delete")
    kwargs.setdefault("ignore_permissions", True)
    return frappe.delete_doc(doctype, name, **kwargs)


def ensure_default_ai_doctype_permissions() -> int:
    if not frappe.db.exists("DocType", "WA Chat Hub Settings"):
        return 0
    settings = frappe.get_single("WA Chat Hub Settings")
    existing = {
        (row.get("doctype_name") or "").strip()
        for row in settings.get("ai_doctype_permissions") or []
        if row.get("doctype_name")
    }
    added = 0
    for doctype, allow_read, allow_write, allow_delete, notes in DEFAULT_AI_DOCTYPE_PERMISSIONS:
        if doctype in existing or not frappe.db.exists("DocType", doctype):
            continue
        settings.append(
            "ai_doctype_permissions",
            {
                "doctype_name": doctype,
                "allow_read": allow_read,
                "allow_write": allow_write,
                "allow_delete": allow_delete,
                "is_active": 1,
                "notes": notes,
            },
        )
        added += 1
    if added:
        settings.flags.ignore_links = True
        settings.save(ignore_permissions=True)
        frappe.db.commit()
    return added


def _log_blocked_access(doctype: str, action: str) -> None:
    operation = getattr(frappe.flags, "wa_chat_hub_service_operation", "") or ""
    conversation = getattr(frappe.flags, "wa_chat_hub_security_conversation", "") or ""
    message = getattr(frappe.flags, "wa_chat_hub_security_message", "") or ""
    channel_account = getattr(frappe.flags, "wa_chat_hub_security_channel_account", "") or ""

    if conversation and not channel_account:
        try:
            channel_account = frappe.db.get_value("Chat Conversation", conversation, "channel_account") or ""
        except Exception:
            channel_account = ""

    try:
        frappe.log_error(
            message=(
                f"Operation: {operation or '-'}\n"
                f"Action: {action}\n"
                f"DocType: {doctype}\n"
                f"Conversation: {conversation or '-'}\n"
                f"Message: {message or '-'}"
            ),
            title="WA Chat Hub AI DocType Permission Blocked",
        )
    except Exception:
        pass

    if not conversation:
        return

    try:
        if not frappe.db.exists("DocType", "Chat Action Log"):
            return
        action_type = "ERP Write" if action in {"write", "delete"} else "ERP Read"
        details = (
            f"Blocked AI/service {action or '-'} access for DocType {doctype or '-'}."
            f" Operation: {operation or '-'}."
        )
        request_json = {
            "action": action or "",
            "doctype": doctype or "",
            "operation": operation or "",
            "message": message or "",
        }
        doc = frappe.get_doc(
            {
                "doctype": "Chat Action Log",
                "conversation": conversation,
                "channel_account": channel_account,
                "action_source": "AI",
                "action_type": action_type,
                "action_name": "AI DocType Permission Blocked",
                "status": "Failed",
                "actor": frappe.session.user if getattr(frappe, "session", None) else "",
                "reference_doctype": "DocType",
                "reference_name": doctype,
                "details": details,
                "request_json": json.dumps(request_json, ensure_ascii=True),
            }
        )
        doc.insert(ignore_permissions=True)
    except Exception:
        pass
