# AI Settings, Knowledge Base, and MCP Configuration

## Goal
Make `wa_chat_hub` closer to plug-and-play by including an in-app control plane for AI behavior, knowledge sources, ERP access, and MCP server configuration.

## Added Components
- `WA Chat Hub Settings` (single doctype)
- `WA AI Knowledge Base`
- `WA MCP Server`
- `WA AI Tool Permission`
- settings page scaffold: `wa-chat-hub-settings`
- settings API: `get_ai_settings_context`

## What this enables
- configure AI mode and guardrails in ERPNext
- register knowledge sources by department
- allow/disallow ERP read/write and MCP access
- configure MCP server endpoints and auth metadata
- declare tool permissions for AI/agent workflows

## Intended production use
- SOPs and FAQs can be uploaded or linked as KB entries
- MCP servers can be registered per department or globally
- AI agent behavior can be tightened for medical-risk flows
