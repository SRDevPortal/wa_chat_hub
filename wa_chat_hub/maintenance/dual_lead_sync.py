from __future__ import annotations

import frappe

from wa_chat_hub.ai.lead_scoring import score_and_sync_conversation
from wa_chat_hub.security import set_ai_security_context, set_service_user_context
from wa_chat_hub.services import _ensure_inbound_lead_pair, normalize_phone


def backfill_whatsapp_lead_pairs() -> dict[str, int]:
    """Ensure existing lead-linked WhatsApp conversations have both lead records."""
    _enable_dual_lead_permissions()
    set_service_user_context("dual_lead_backfill")
    rows = frappe.get_all(
        "Chat Conversation",
        fields=[
            "name",
            "contact",
            "channel_account",
            "linked_reference_doctype",
            "linked_reference_name",
            "linked_crm_lead",
        ],
        limit_page_length=0,
    )
    stats = {"checked": 0, "updated": 0, "failed": 0}
    for row in rows:
        if (
            row.linked_reference_doctype not in {"Lead", "CRM Lead"}
            and not row.linked_crm_lead
        ):
            continue
        stats["checked"] += 1
        frappe.db.savepoint("wa_dual_lead_backfill_row")
        try:
            set_ai_security_context(channel_account=row.channel_account)
            contact = frappe.get_doc("Chat Contact", row.contact)
            phone_number = normalize_phone(contact.phone_number)
            if not phone_number:
                continue

            existing_doctype = row.linked_reference_doctype
            existing_name = row.linked_reference_name
            if row.linked_crm_lead:
                existing_doctype = "CRM Lead"
                existing_name = row.linked_crm_lead
            pair = _ensure_inbound_lead_pair(
                phone_number=phone_number,
                display_name=contact.display_name or phone_number,
                channel_account=row.channel_account,
                existing_doctype=existing_doctype,
                existing_name=existing_name,
            )
            erpnext_lead = pair.get("Lead")
            crm_lead = pair.get("CRM Lead")
            if not erpnext_lead or not crm_lead:
                continue

            contact_updates = {
                "source_doctype": "CRM Lead",
                "source_name": crm_lead,
            }
            if frappe.get_meta("Chat Contact").has_field("linked_lead"):
                contact_updates["linked_lead"] = erpnext_lead
            frappe.db.set_value(
                "Chat Contact",
                contact.name,
                contact_updates,
                update_modified=False,
            )

            conversation_updates = {
                "linked_reference_doctype": "CRM Lead",
                "linked_reference_name": crm_lead,
            }
            if frappe.get_meta("Chat Conversation").has_field("linked_crm_lead"):
                conversation_updates["linked_crm_lead"] = crm_lead
            frappe.db.set_value(
                "Chat Conversation",
                row.name,
                conversation_updates,
                update_modified=False,
            )
            score_and_sync_conversation(row.name)
            stats["updated"] += 1
        except Exception:
            frappe.db.rollback(save_point="wa_dual_lead_backfill_row")
            frappe.log_error(
                frappe.get_traceback(),
                f"WA Dual Lead Backfill Failed: {row.name}",
            )
            stats["failed"] += 1

    frappe.db.commit()
    return stats


def _enable_dual_lead_permissions() -> None:
    """Allow the service user to maintain both lead records on this site."""
    settings = frappe.get_single("WA Chat Hub Settings")
    changed = False
    rows = {
        str(row.doctype_name or "").strip(): row
        for row in settings.get("ai_doctype_permissions") or []
    }
    for doctype in ("Lead", "CRM Lead"):
        row = rows.get(doctype)
        if not row:
            settings.append(
                "ai_doctype_permissions",
                {
                    "doctype_name": doctype,
                    "allow_read": 1,
                    "allow_write": 1,
                    "allow_delete": 0,
                    "is_active": 1,
                    "notes": "Create and synchronize WhatsApp leads in both lead lists.",
                },
            )
            changed = True
            continue
        for fieldname in ("allow_read", "allow_write", "is_active"):
            if not row.get(fieldname):
                row.set(fieldname, 1)
                changed = True
    if changed:
        # Some older local rows can reference optional DocTypes that are no
        # longer installed. They must not block this targeted permission fix.
        settings.flags.ignore_links = True
        settings.save(ignore_permissions=True)
        frappe.db.commit()
