from __future__ import annotations

import re

import frappe
from frappe import _

from wa_chat_hub.clinic_compat import get_crm_lead_pqc


CRM_LEAD_DOCTYPES = {"CRM Lead", "Lead"}


def _user(user: str | None = None) -> str:
    return user or frappe.session.user


def _has_crm_lead_doctype() -> bool:
    try:
        return bool(frappe.db.exists("DocType", "CRM Lead"))
    except Exception:
        return False


def _role_bypass(user: str | None = None) -> bool:
    user = _user(user)
    if user == "Administrator":
        return True
    roles = set(frappe.get_roles(user) or [])
    return bool(roles.intersection({"System Manager", "WA Chat Manager"}))


def _qualify_crm_lead_condition(condition: str, alias: str) -> str:
    """Rewrite CRM Lead PQC SQL so it can run inside a joined/subquery alias."""
    if not condition:
        return ""
    qualified = condition.replace("`tabCRM Lead`.", f"`{alias}`.")
    qualified = qualified.replace("`tabCRM Lead`", f"`{alias}`")
    qualified = re.sub(r"(?<![`.\w])name(?![`.\w])", f"`{alias}`.`name`", qualified)
    return qualified


def crm_lead_permission_condition(user: str | None = None, alias: str = "wa_lead") -> str:
    """
    Return CRM Lead permission SQL for the user.

    Empty string means unrestricted. "1=0" means no CRM Lead access.
    This intentionally reuses the host app's CRM Lead permission query hook when present.
    """
    user = _user(user)
    if _role_bypass(user):
        return ""
    if not _has_crm_lead_doctype():
        return "1=0"

    crm_lead_pqc = get_crm_lead_pqc()
    if crm_lead_pqc:
        return _qualify_crm_lead_condition(crm_lead_pqc(user) or "", alias)

    if frappe.db.has_column("CRM Lead", "lead_owner"):
        return f"`{alias}`.`lead_owner` = {frappe.db.escape(user)}"
    return "1=0"


def has_unrestricted_chat_access(user: str | None = None) -> bool:
    user = _user(user)
    cache = getattr(frappe.local, "wa_chat_hub_unrestricted_chat_access", None)
    if cache is None:
        cache = frappe.local.wa_chat_hub_unrestricted_chat_access = {}
    if user not in cache:
        cache[user] = _role_bypass(user) or crm_lead_permission_condition(user) == ""
    return cache[user]


def get_conversation_crm_lead(conversation_or_row) -> str | None:
    if isinstance(conversation_or_row, (str, int)):
        fields = ["linked_crm_lead", "linked_reference_doctype", "linked_reference_name"]
        row = frappe.db.get_value("Chat Conversation", str(conversation_or_row), fields, as_dict=True)
    elif callable(getattr(conversation_or_row, "as_dict", None)):
        row = conversation_or_row.as_dict()
    else:
        row = conversation_or_row or {}

    if not row:
        return None

    linked_crm_lead = row.get("linked_crm_lead")
    if linked_crm_lead:
        return linked_crm_lead

    if row.get("linked_reference_doctype") in CRM_LEAD_DOCTYPES:
        return row.get("linked_reference_name")
    return None


def can_read_crm_lead(lead_name: str | None, user: str | None = None) -> bool:
    user = _user(user)
    if has_unrestricted_chat_access(user):
        return True
    if not lead_name or not _has_crm_lead_doctype() or not frappe.db.exists("CRM Lead", lead_name):
        return False
    try:
        lead = frappe.get_doc("CRM Lead", lead_name)
        return bool(frappe.has_permission("CRM Lead", "read", doc=lead, user=user))
    except Exception:
        return False


def can_read_conversation(conversation_or_row, user: str | None = None) -> bool:
    user = _user(user)
    if has_unrestricted_chat_access(user):
        return True
    lead_name = get_conversation_crm_lead(conversation_or_row)
    return can_read_crm_lead(lead_name, user=user)


def ensure_can_read_conversation(conversation_or_row, user: str | None = None) -> None:
    if not can_read_conversation(conversation_or_row, user=user):
        frappe.throw(_("Not permitted to access this WhatsApp conversation"), frappe.PermissionError)


def filter_accessible_conversation_rows(rows: list, user: str | None = None) -> list:
    user = _user(user)
    if has_unrestricted_chat_access(user):
        return rows

    lead_access_cache: dict[str, bool] = {}
    visible = []
    for row in rows:
        lead_name = get_conversation_crm_lead(row)
        if not lead_name:
            continue
        if lead_name not in lead_access_cache:
            lead_access_cache[lead_name] = can_read_crm_lead(lead_name, user=user)
        if lead_access_cache[lead_name]:
            visible.append(row)
    return visible


def filter_accessible_reference_names(reference_doctype: str, names: list[str], user: str | None = None) -> list[str]:
    user = _user(user)
    if has_unrestricted_chat_access(user):
        return names
    if reference_doctype not in CRM_LEAD_DOCTYPES:
        return []
    return [name for name in names if can_read_crm_lead(name, user=user)]


def conversation_access_sql_condition(conversation_alias: str = "c", user: str | None = None) -> str:
    user = _user(user)
    if has_unrestricted_chat_access(user):
        return "1=1"

    lead_condition = crm_lead_permission_condition(user, alias="wa_lead")
    if not lead_condition or lead_condition == "1=0":
        return "1=0"

    linked_crm_lead = "1=0"
    if frappe.db.has_column("Chat Conversation", "linked_crm_lead"):
        linked_crm_lead = f"""
            (
                IFNULL(`{conversation_alias}`.`linked_crm_lead`, '') != ''
                AND EXISTS (
                    SELECT 1
                    FROM `tabCRM Lead` `wa_lead`
                    WHERE `wa_lead`.`name` = `{conversation_alias}`.`linked_crm_lead`
                      AND ({lead_condition})
                )
            )
        """

    linked_reference = "1=0"
    if frappe.db.has_column("Chat Conversation", "linked_reference_doctype") and frappe.db.has_column(
        "Chat Conversation", "linked_reference_name"
    ):
        linked_reference = f"""
            (
                `{conversation_alias}`.`linked_reference_doctype` IN ('CRM Lead', 'Lead')
                AND IFNULL(`{conversation_alias}`.`linked_reference_name`, '') != ''
                AND EXISTS (
                    SELECT 1
                    FROM `tabCRM Lead` `wa_lead`
                    WHERE `wa_lead`.`name` = `{conversation_alias}`.`linked_reference_name`
                      AND ({lead_condition})
                )
            )
        """

    return f"(({linked_crm_lead}) OR ({linked_reference}))"


def chat_conversation_pqc(user: str | None = None) -> str:
    return conversation_access_sql_condition("tabChat Conversation", user=user)


def chat_conversation_has_permission(doc, user: str | None = None, ptype: str | None = None) -> bool:
    return can_read_conversation(doc, user=user)


def contact_access_sql_condition(contact_alias: str = "tabChat Contact", user: str | None = None) -> str:
    user = _user(user)
    if has_unrestricted_chat_access(user):
        return "1=1"

    lead_condition = crm_lead_permission_condition(user, alias="wa_lead")
    if not lead_condition or lead_condition == "1=0":
        return "1=0"

    linked_crm_lead = "1=0"
    if frappe.db.has_column("Chat Conversation", "linked_crm_lead"):
        linked_crm_lead = f"""
            (
                IFNULL(`wa_conversation`.`linked_crm_lead`, '') != ''
                AND EXISTS (
                    SELECT 1
                    FROM `tabCRM Lead` `wa_lead`
                    WHERE `wa_lead`.`name` = `wa_conversation`.`linked_crm_lead`
                      AND ({lead_condition})
                )
            )
        """

    linked_reference = "1=0"
    if frappe.db.has_column("Chat Conversation", "linked_reference_doctype") and frappe.db.has_column(
        "Chat Conversation", "linked_reference_name"
    ):
        linked_reference = f"""
            (
                `wa_conversation`.`linked_reference_doctype` IN ('CRM Lead', 'Lead')
                AND IFNULL(`wa_conversation`.`linked_reference_name`, '') != ''
                AND EXISTS (
                    SELECT 1
                    FROM `tabCRM Lead` `wa_lead`
                    WHERE `wa_lead`.`name` = `wa_conversation`.`linked_reference_name`
                      AND ({lead_condition})
                )
            )
        """

    return f"""
        EXISTS (
            SELECT 1
            FROM `tabChat Conversation` `wa_conversation`
            WHERE `wa_conversation`.`contact` = `{contact_alias}`.`name`
              AND (({linked_crm_lead}) OR ({linked_reference}))
        )
    """


def chat_contact_pqc(user: str | None = None) -> str:
    return contact_access_sql_condition("tabChat Contact", user=user)


def can_read_contact(contact_or_doc, user: str | None = None) -> bool:
    user = _user(user)
    if has_unrestricted_chat_access(user):
        return True

    if isinstance(contact_or_doc, str):
        contact = contact_or_doc
    elif isinstance(contact_or_doc, dict):
        contact = contact_or_doc.get("name")
    else:
        contact = getattr(contact_or_doc, "name", None)

    if not contact:
        return False

    rows = frappe.get_all(
        "Chat Conversation",
        filters={"contact": contact},
        fields=["name", "linked_crm_lead", "linked_reference_doctype", "linked_reference_name"],
        limit_page_length=50,
        ignore_permissions=True,
    )
    return any(can_read_conversation(row, user=user) for row in rows)


def chat_contact_has_permission(doc, user: str | None = None, ptype: str | None = None) -> bool:
    return can_read_contact(doc, user=user)


def chat_message_pqc(user: str | None = None) -> str:
    condition = conversation_access_sql_condition("wa_conversation", user=user)
    return f"""
        EXISTS (
            SELECT 1
            FROM `tabChat Conversation` `wa_conversation`
            WHERE `wa_conversation`.`name` = `tabChat Message`.`conversation`
              AND ({condition})
        )
    """


def chat_message_has_permission(doc, user: str | None = None, ptype: str | None = None) -> bool:
    conversation = None
    if isinstance(doc, str):
        conversation = frappe.db.get_value("Chat Message", doc, "conversation")
    elif doc:
        conversation = doc.get("conversation") if isinstance(doc, dict) else getattr(doc, "conversation", None)
    return can_read_conversation(conversation, user=user)
