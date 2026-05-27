"""Create or update the WhatsApp workspace for WA Chat Hub."""

from __future__ import annotations

import json

import frappe


WORKSPACE_ICON = "message"


def _shortcut(label, link_to=None, url=None, shortcut_type="DocType", icon=None):
    return {
        "label": label,
        "link_to": link_to,
        "url": url,
        "type": shortcut_type,
        "icon": icon,
    }


def _header(block_id, text):
    return {
        "id": block_id,
        "type": "header",
        "data": {
            "text": text,
            "level": 4,
            "col": 12,
        },
    }


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

    sections = [
        (
            "WhatsApp Operations Hub",
            [
                _shortcut("WA Chat Hub", url="/app/wa-chat-hub?scope=all", shortcut_type="URL", icon="home"),
                _shortcut("Chat Conversation", "Chat Conversation", icon="message"),
                _shortcut("Chat Contact", "Chat Contact", icon="user"),
                _shortcut("Chat Message", "Chat Message", icon="comment"),
            ],
        ),
        (
            "Messaging Setup",
            [
                _shortcut("Chat Channel Account", "Chat Channel Account", icon="settings"),
                _shortcut("Chat Channel Session", "Chat Channel Session", icon="phone"),
                _shortcut("WA Channel Pipeline Map", "WA Channel Pipeline Map", icon="branch"),
                _shortcut("WA Channel Context", "WA Channel Context", icon="file"),
                _shortcut("WA Chat Hub Settings", "WA Chat Hub Settings", icon="settings"),
            ],
        ),
        (
            "Automation and AI",
            [
                _shortcut("WA AI Knowledge Base", "WA AI Knowledge Base", icon="book"),
                _shortcut("WA LLM Provider", "WA LLM Provider", icon="cpu"),
                _shortcut("WA MCP Server", "WA MCP Server", icon="server"),
                _shortcut("WA MCP Tool Endpoint", "WA MCP Tool Endpoint", icon="tool"),
                _shortcut("WA AI Tool Permission", "WA AI Tool Permission", icon="lock"),
                _shortcut("Chat AI Suggestion", "Chat AI Suggestion", icon="sparkles"),
                _shortcut("WA Lead AI Insight", "WA Lead AI Insight", icon="chart"),
                _shortcut("WA Lead OCR Result", "WA Lead OCR Result", icon="file-search"),
            ],
        ),
        (
            "Operations Admin",
            [
                _shortcut("Chat Assignment Rule", "Chat Assignment Rule", icon="assign"),
                _shortcut("Chat Queue Event", "Chat Queue Event", icon="list"),
                _shortcut("Chat Action Log", "Chat Action Log", icon="activity"),
                _shortcut("Chat Contact Channel Profile", "Chat Contact Channel Profile", icon="contact"),
            ],
        ),
    ]

    content_blocks = []
    shortcut_index = 0
    for section_index, (section_title, shortcuts) in enumerate(sections):
        content_blocks.append(_header(f"header_{section_index}", section_title))

        for shortcut in shortcuts:
            if shortcut["type"] == "DocType" and not frappe.db.exists("DocType", shortcut["link_to"]):
                continue

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
                    "id": f"sc_{shortcut_index}",
                    "type": "shortcut",
                    "data": {
                        "shortcut_name": shortcut["label"],
                        "col": 3,
                    },
                }
            )
            shortcut_index += 1

    doc.content = json.dumps(content_blocks)
    doc.flags.ignore_links = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"Workspace '{workspace_name}' created / updated successfully.")
