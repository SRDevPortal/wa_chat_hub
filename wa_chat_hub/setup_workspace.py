"""Create or update the WhatsApp workspace for WA Chat Hub."""

from __future__ import annotations

import json

import frappe


WORKSPACE_ICON = "message"
WORKSPACE_NAME = "WhatsApp"
LEGACY_WORKSPACE_NAMES = ("WhatsApp Admin",)


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


def _get_operations_shortcuts():
    return [
        _shortcut("WA Chat Hub", url="/app/wa-chat-hub?scope=all", shortcut_type="URL", icon="home"),
        _shortcut("Chat Conversation", "Chat Conversation", icon="message"),
        _shortcut("Chat Contact", "Chat Contact", icon="user"),
        _shortcut("Chat Message", "Chat Message", icon="comment"),
    ]


def _get_admin_link_cards():
    return [
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
                _shortcut("WA AI Agent Profile", "WA AI Agent Profile", icon="cpu"),
                _shortcut("WA AI Department Profile", "WA AI Department Profile", icon="branch"),
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


def _doctype_exists(shortcut):
    return shortcut["type"] != "DocType" or frappe.db.exists("DocType", shortcut["link_to"])


def _append_shortcut(doc, shortcut):
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


def _append_link_card(doc, card_label, shortcuts):
    visible_shortcuts = [shortcut for shortcut in shortcuts if _doctype_exists(shortcut)]
    if not visible_shortcuts:
        return False

    doc.append(
        "links",
        {
            "label": card_label,
            "type": "Card Break",
            "icon": "folder-normal",
        },
    )

    for shortcut in visible_shortcuts:
        doc.append(
            "links",
            {
                "label": shortcut["label"],
                "type": "Link",
                "link_type": shortcut["type"],
                "link_to": shortcut["link_to"],
            },
        )

    return True


def run():
    previous_in_patch = frappe.flags.in_patch
    frappe.flags.in_patch = True

    try:
        remove_legacy_workspaces()

        if frappe.db.exists("Workspace", WORKSPACE_NAME):
            doc = frappe.get_doc("Workspace", WORKSPACE_NAME)
            doc.links = []
            doc.shortcuts = []
        else:
            doc = frappe.new_doc("Workspace")
            doc.name = WORKSPACE_NAME

        doc.label = WORKSPACE_NAME
        doc.title = WORKSPACE_NAME

        doc.module = "WA Chat Hub"
        doc.icon = WORKSPACE_ICON
        doc.public = 1
        doc.is_standard = 1

        content_blocks = [_header("header_main", "WhatsApp Operations Hub")]
        shortcut_index = 0
        for shortcut in _get_operations_shortcuts():
            if not _doctype_exists(shortcut):
                continue

            _append_shortcut(doc, shortcut)
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

        for card_index, (card_label, shortcuts) in enumerate(_get_admin_link_cards()):
            if _append_link_card(doc, card_label, shortcuts):
                content_blocks.append(
                    {
                        "id": f"card_{card_index}",
                        "type": "card",
                        "data": {
                            "card_name": card_label,
                            "col": 4,
                        },
                    }
                )

        doc.content = json.dumps(content_blocks)
        doc.flags.ignore_links = True
        doc.save(ignore_permissions=True)
    finally:
        frappe.flags.in_patch = previous_in_patch


def remove_legacy_workspaces():
    for workspace_name in LEGACY_WORKSPACE_NAMES:
        if not frappe.db.exists("Workspace", workspace_name):
            continue

        module = frappe.db.get_value("Workspace", workspace_name, "module")
        if module != "WA Chat Hub":
            continue

        frappe.delete_doc(
            "Workspace",
            workspace_name,
            ignore_permissions=True,
            force=True,
            delete_permanently=True,
        )
