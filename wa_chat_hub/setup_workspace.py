"""Create or update the WhatsApp workspace for WA Chat Hub."""

from __future__ import annotations

import json

import frappe


WORKSPACE_ICON = "message"


def run():
    frappe.flags.in_patch = True

    workspace_name = "WhatsApp"
    if frappe.db.exists("Workspace", workspace_name):
        doc = frappe.get_doc("Workspace", workspace_name)
        doc.links = []
        doc.shortcuts = []
    else:
        doc = frappe.new_doc("Workspace")
        doc.name = workspace_name
        doc.label = workspace_name
        doc.title = workspace_name

    doc.module = "WA Chat Hub"
    doc.icon = WORKSPACE_ICON
    doc.public = 1
    doc.is_standard = 1

    content_blocks = [
        {
            "id": "header_main",
            "type": "header",
            "data": {
                "text": "WhatsApp Operations Hub",
                "level": 4,
                "col": 12,
            },
        }
    ]

    shortcuts = [
        {"label": "WA Chat Hub", "url": "/app/wa-chat-hub?scope=all", "type": "URL", "icon": "home"},
        {"label": "Chat Conversation", "link_to": "Chat Conversation", "type": "DocType", "icon": "message"},
        {"label": "Chat Contact", "link_to": "Chat Contact", "type": "DocType", "icon": "user"},
        {"label": "Chat Message", "link_to": "Chat Message", "type": "DocType", "icon": "comment"},
    ]

    for index, shortcut in enumerate(shortcuts):
        doc.append(
            "shortcuts",
            {
                "label": shortcut["label"],
                "link_to": shortcut.get("link_to"),
                "url": shortcut.get("url"),
                "type": shortcut["type"],
                "icon": shortcut.get("icon"),
            },
        )
        content_blocks.append(
            {
                "id": f"sc_{index}",
                "type": "shortcut",
                "data": {
                    "shortcut_name": shortcut["label"],
                    "col": 4,
                },
            }
        )

    doc.content = json.dumps(content_blocks)
    doc.flags.ignore_links = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"Workspace '{workspace_name}' created / updated successfully.")
