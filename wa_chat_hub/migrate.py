from __future__ import annotations

from pathlib import Path

import frappe


MODULE = "wa_chat_hub"


def after_migrate() -> None:
    """Keep WA Chat Hub standard doctypes and workspace synced after migrate."""
    sync_standard_doctypes()
    try:
        from wa_chat_hub.setup_workspace import run as setup_workspace

        setup_workspace()
    except Exception:
        frappe.log_error(frappe.get_traceback(), "WA Chat Hub Workspace Sync Failed")


def sync_standard_doctypes() -> None:
    doctype_dir = Path(__file__).parent / MODULE / "doctype"
    if not doctype_dir.exists():
        return

    for json_file in sorted(doctype_dir.glob("*/*.json")):
        doctype_name = json_file.parent.name
        try:
            frappe.reload_doc(MODULE, "doctype", doctype_name, force=True)
        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"WA Chat Hub DocType Sync Failed: {doctype_name}",
            )


def get_doctype_names() -> list[str]:
    return frappe.get_all(
        "DocType",
        filters={"module": "WA Chat Hub"},
        pluck="name",
        order_by="name",
    )


def validate_whatsapp_workspace_links() -> dict[str, object]:
    workspace = frappe.get_doc("Workspace", "WhatsApp")

    def check(target_type: str, target: str | None) -> bool:
        if not target:
            return False
        if target_type not in {"DocType", "Page"}:
            return True
        return bool(frappe.db.exists(target_type, target))

    shortcuts = [
        {
            "label": row.label,
            "type": row.type,
            "target": row.get("link_to") or row.label,
            "exists": check(row.type, row.get("link_to") or row.label),
        }
        for row in workspace.shortcuts
    ]
    links = [
        {
            "label": row.label,
            "type": row.link_type,
            "target": row.get("link_to") or row.label,
            "exists": check(row.link_type, row.get("link_to") or row.label),
        }
        for row in workspace.links
    ]
    return {
        "workspace": workspace.name,
        "title": workspace.title,
        "route": "/app/whatsapp",
        "shortcuts": shortcuts,
        "links": links,
        "broken": [row for row in shortcuts + links if not row["exists"]],
    }


def inspect_whatsapp_workspace_rows() -> dict[str, object]:
    workspace = frappe.get_doc("Workspace", "WhatsApp")
    return {
        "shortcuts": [row.as_dict() for row in workspace.shortcuts],
        "links": [row.as_dict() for row in workspace.links],
    }


def inspect_workspace_child_meta() -> dict[str, list[dict[str, str | None]]]:
    return {
        doctype: [
            {
                "fieldname": field.fieldname,
                "fieldtype": field.fieldtype,
                "options": field.options,
            }
            for field in frappe.get_meta(doctype).fields
        ]
        for doctype in ("Workspace Shortcut", "Workspace Link")
    }


def reload_core_workspace_child_doctypes() -> str:
    frappe.reload_doc("desk", "doctype", "workspace_shortcut", force=True)
    frappe.reload_doc("desk", "doctype", "workspace_link", force=True)
    return "Reloaded Workspace Shortcut and Workspace Link"


def remove_duplicate_whatsapp_shortcuts() -> dict[str, object]:
    workspace = frappe.get_doc("Workspace", "WhatsApp")
    seen: set[tuple[str, str | None]] = set()
    duplicates = []
    clean_shortcuts = []

    for row in workspace.shortcuts:
        key = (row.type, row.get("link_to") or row.label)
        if key in seen:
            duplicates.append(row.as_dict())
            continue
        seen.add(key)
        clean_shortcuts.append(row)

    if duplicates:
        workspace.set("shortcuts", clean_shortcuts)
        workspace.save(ignore_permissions=True)
        frappe.db.commit()

    return {
        "removed": len(duplicates),
        "duplicates": duplicates,
        "remaining": len(clean_shortcuts),
    }


def audit_whatsapp_workspace_duplicates() -> dict[str, object]:
    workspace = frappe.get_doc("Workspace", "WhatsApp")

    def duplicates(rows, key_fn):
        buckets: dict[tuple[str | None, ...], list[dict[str, object]]] = {}
        for row in rows:
            buckets.setdefault(key_fn(row), []).append(
                {
                    "label": row.label,
                    "type": row.get("type"),
                    "link_type": row.get("link_type"),
                    "link_to": row.get("link_to"),
                }
            )
        return {str(key): value for key, value in buckets.items() if len(value) > 1}

    return {
        "shortcut_count": len(workspace.shortcuts),
        "link_count": len(workspace.links),
        "duplicate_shortcut_targets": duplicates(
            workspace.shortcuts,
            lambda row: (row.get("type"), row.get("link_to")),
        ),
        "duplicate_link_targets": duplicates(
            workspace.links,
            lambda row: (row.get("link_type"), row.get("link_to")),
        ),
        "duplicate_visible_labels": duplicates(
            list(workspace.shortcuts) + list(workspace.links),
            lambda row: (row.label,),
        ),
    }


def create_demo_data() -> dict[str, object]:
    frappe.set_user("Administrator")

    channel_name = "Demo WhatsApp Official"
    if not frappe.db.exists("Chat Channel Account", channel_name):
        channel = frappe.get_doc(
            {
                "doctype": "Chat Channel Account",
                "account_name": channel_name,
                "channel_type": "Official WhatsApp",
                "phone_number": "919900001111",
                "phone_id": "demo_phone_id",
                "waba_id": "demo_waba_id",
                "connector_status": "Active",
                "is_active": 1,
            }
        )
        channel.insert(ignore_permissions=True)
    else:
        channel = frappe.get_doc("Chat Channel Account", channel_name)

    samples = [
        {
            "phone": "919876543210",
            "name": "Ananya Sharma",
            "priority": "High",
            "messages": [
                ("Inbound", "Customer", "Hi, I want to book a consultation for tomorrow."),
                ("Outbound", "Agent", "Sure. May I know the preferred department and time?"),
                ("Inbound", "Customer", "Dermatology, around 11 AM if available."),
            ],
        },
        {
            "phone": "918888777666",
            "name": "Rohan Mehta",
            "priority": "Medium",
            "messages": [
                ("Inbound", "Customer", "Can you share the invoice for my last visit?"),
                ("Outbound", "Agent", "Yes, I am checking your account and will share it shortly."),
            ],
        },
        {
            "phone": "917777666555",
            "name": "Priya Nair",
            "priority": "Urgent",
            "messages": [
                ("Inbound", "Customer", "My prescription medicine is not showing in the order."),
                ("Outbound", "Agent", "I will verify this with the pharmacy team right away."),
            ],
        },
    ]

    created = {"contacts": 0, "conversations": 0, "messages": 0}

    for sample in samples:
        contact_name = sample["phone"]
        if not frappe.db.exists("Chat Contact", contact_name):
            contact = frappe.get_doc(
                {
                    "doctype": "Chat Contact",
                    "phone_number": sample["phone"],
                    "display_name": sample["name"],
                    "source_doctype": "Demo",
                    "source_name": "WA Chat Hub Demo",
                }
            )
            contact.insert(ignore_permissions=True)
            created["contacts"] += 1
        else:
            contact = frappe.get_doc("Chat Contact", contact_name)
            contact.display_name = sample["name"]
            contact.save(ignore_permissions=True)

        conversation_name = frappe.db.get_value(
            "Chat Conversation",
            {
                "channel_account": channel.name,
                "contact": contact.name,
                "status": "Open",
            },
            "name",
        )
        if conversation_name:
            conversation = frappe.get_doc("Chat Conversation", conversation_name)
        else:
            conversation = frappe.get_doc(
                {
                    "doctype": "Chat Conversation",
                    "channel_account": channel.name,
                    "contact": contact.name,
                    "status": "Open",
                    "priority": sample["priority"],
                    "assigned_to": "Administrator",
                    "unread_count": 0,
                }
            )
            conversation.insert(ignore_permissions=True)
            created["conversations"] += 1

        existing_demo_messages = frappe.get_all(
            "Chat Message",
            filters={"conversation": conversation.name, "raw_payload": ["like", "%WA Chat Hub Demo%"]},
            pluck="name",
        )
        if not existing_demo_messages:
            unread_count = 0
            last_preview = ""
            for idx, (direction, sender_type, body) in enumerate(sample["messages"], start=1):
                message = frappe.get_doc(
                    {
                        "doctype": "Chat Message",
                        "conversation": conversation.name,
                        "direction": direction,
                        "sender_type": sender_type,
                        "content_type": "Text",
                        "body": body,
                        "delivery_status": "Delivered" if direction == "Outbound" else "Read",
                        "channel_message_id": f"demo-{contact.name}-{idx}",
                        "raw_payload": frappe.as_json({"source": "WA Chat Hub Demo", "index": idx}),
                    }
                )
                message.insert(ignore_permissions=True)
                created["messages"] += 1
                last_preview = body
                if direction == "Inbound":
                    unread_count += 1

            conversation.last_message_preview = last_preview[:500]
            conversation.unread_count = unread_count
            conversation.priority = sample["priority"]
            conversation.assigned_to = "Administrator"
            conversation.save(ignore_permissions=True)

    frappe.db.commit()
    return {
        "success": True,
        "created": created,
        "channel_account": channel.name,
        "conversation_count": frappe.db.count("Chat Conversation", {"channel_account": channel.name}),
    }
