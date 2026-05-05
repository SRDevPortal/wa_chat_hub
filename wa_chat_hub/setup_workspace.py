"""
Setup script: creates the "WhatsApp" workspace in Frappe with shortcuts
to all WA Chat Hub doctypes and the WA Chat Hub page.

Run with:
    bench --site <site> execute wa_chat_hub.setup_workspace.run
"""

import frappe
import json


def run():
    frappe.flags.in_patch = True  # allows creating standard workspaces

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

    # Content header block (Frappe v14/v15 editor format)
    content_blocks = [
        {
            "id": "header_main",
            "type": "header",
            "data": {
                "text": "WhatsApp Operations Hub",
                "level": 4,
                "col": 12
            },
        }
    ]

    # ── Shortcuts (icon tiles at the top of the workspace) ────────────────────
    shortcuts = [
        {"label": "WA Chat Hub", "link_to": "wa-chat-hub", "type": "Page", "icon": "home"},
        {"label": "Chat Conversation", "link_to": "Chat Conversation", "type": "DocType", "icon": "chat"},
        {"label": "Chat Contact", "link_to": "Chat Contact", "type": "DocType", "icon": "user"},
        {"label": "Chat Message", "link_to": "Chat Message", "type": "DocType", "icon": "comment"},
        {"label": "Chat Channel Account", "link_to": "Chat Channel Account", "type": "DocType", "icon": "users"},
        {"label": "Chat Assignment Rule", "link_to": "Chat Assignment Rule", "type": "DocType", "icon": "filter"},
        {"label": "Chat Queue Event", "link_to": "Chat Queue Event", "type": "DocType", "icon": "list"},
        {"label": "Chat AI Suggestion", "link_to": "Chat AI Suggestion", "type": "DocType", "icon": "star"},
        {"label": "WA Knowledge Base", "link_to": "WA Knowledge Base", "type": "DocType", "icon": "book"},
        {"label": "WA AI Knowledge Base", "link_to": "WA AI Knowledge Base", "type": "DocType", "icon": "book-open"},
        {"label": "WA LLM Provider", "link_to": "WA LLM Provider", "type": "DocType", "icon": "cpu"},
        {"label": "WA MCP Server", "link_to": "WA MCP Server", "type": "DocType", "icon": "server"},
        {"label": "WA MCP Tool Endpoint", "link_to": "WA MCP Tool Endpoint", "type": "DocType", "icon": "server"},
        {"label": "WA AI Tool Permission", "link_to": "WA AI Tool Permission", "type": "DocType", "icon": "lock"},
        {"label": "WA Chat Hub Settings", "link_to": "WA Chat Hub Settings", "type": "DocType", "icon": "settings"},
        {"label": "Chat Channel Session", "link_to": "Chat Channel Session", "type": "DocType", "icon": "clock"},
        {"label": "Chat Action Log", "link_to": "Chat Action Log", "type": "DocType", "icon": "history"},
    ]

    for i, s in enumerate(shortcuts):
        # Add to shortcuts table
        doc.append("shortcuts", {
            "label": s["label"],
            "link_to": s["link_to"],
            "type": s["type"],
            "icon": s.get("icon")
        })
        # Add to content blocks JSON for rendering in the new desk
        content_blocks.append({
            "id": f"sc_{i}",
            "type": "shortcut",
            "data": {
                "shortcut_name": s["label"],
                "col": 4
            }
        })

    doc.content = json.dumps(content_blocks)

    # ── Links section (same items, shown as list cards) ───────────────────────
    doc.flags.ignore_links = True
    doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"✅  Workspace '{workspace_name}' created / updated successfully.")
