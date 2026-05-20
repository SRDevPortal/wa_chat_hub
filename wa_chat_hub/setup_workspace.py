"""Create or update the WhatsApp workspace for WA Chat Hub."""

from __future__ import annotations

import json

import frappe


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
        {"label": "WA Chat Hub", "link_to": "wa-chat-hub", "type": "Page", "icon": "home"},
        {"label": "Chat Conversation", "link_to": "Chat Conversation", "type": "DocType", "icon": "chat"},
        {"label": "Chat Contact", "link_to": "Chat Contact", "type": "DocType", "icon": "user"},
        {"label": "Chat Contact Channel Profile", "link_to": "Chat Contact Channel Profile", "type": "DocType", "icon": "users"},
        {"label": "Chat Message", "link_to": "Chat Message", "type": "DocType", "icon": "comment"},
        {"label": "Chat Channel Account", "link_to": "Chat Channel Account", "type": "DocType", "icon": "users"},
        {"label": "WA Channel Context", "link_to": "WA Channel Context", "type": "DocType", "icon": "branch"},
        {"label": "Chat Assignment Rule", "link_to": "Chat Assignment Rule", "type": "DocType", "icon": "filter"},
        {"label": "Chat AI Suggestion", "link_to": "Chat AI Suggestion", "type": "DocType", "icon": "star"},
        {"label": "WA AI Knowledge Base", "link_to": "WA AI Knowledge Base", "type": "DocType", "icon": "book-open"},
        {"label": "WA LLM Provider", "link_to": "WA LLM Provider", "type": "DocType", "icon": "cpu"},
        {"label": "WA Lead AI Insight", "link_to": "WA Lead AI Insight", "type": "DocType", "icon": "star"},
        {"label": "WA Lead OCR Result", "link_to": "WA Lead OCR Result", "type": "DocType", "icon": "file"},
        {"label": "WA Chat Hub Settings", "link_to": "WA Chat Hub Settings", "type": "DocType", "icon": "settings"},
    ]

    for index, shortcut in enumerate(shortcuts):
        doc.append(
            "shortcuts",
            {
                "label": shortcut["label"],
                "link_to": shortcut["link_to"],
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
