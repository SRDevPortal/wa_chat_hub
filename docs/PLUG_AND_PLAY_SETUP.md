# Plug-and-Play Setup Guide for WA Chat Hub

## Goal
Make deployment straightforward once installed on ERPNext/Frappe.

## In-app setup sequence
1. Open `WA Chat Hub Settings`
   - enable AI summary / draft
   - configure autopilot mode
   - set default model
   - define medical guardrail and escalation policy
   - enable ERP read/write and MCP access as needed

2. Add Knowledge Base entries
   - FAQs
   - SOPs
   - department playbooks
   - treatment safety guides
   - API specs

3. Add MCP Servers
   - register transport and auth metadata
   - set scope (global / department / agent)
   - define allowed tools

4. Create Chat Channel Accounts
   - one per official or personal WhatsApp number
   - map each to department
   - mark active

5. Optional: Create Assignment Rules
   - route by department/account/priority

## Runtime integration points
- inbound connector calls `wa_chat_hub.api.connector.ingest_connector_event`
- direct app ingestion calls `wa_chat_hub.api.chat.ingest_message`
- desk page reads conversation APIs directly
- send action calls `wa_chat_hub.api.runtime.send_reply`
- AI summary/draft calls AI APIs directly
- MCP runtime invocation calls `wa_chat_hub.api.runtime.call_mcp_tool`

## Important note
True plug-and-play still requires deployment-time runtime wiring for:
- official WhatsApp provider webhook auth
- unofficial QR/websocket session daemon
- actual outbound delivery worker
- real MCP execution bridge
- production AI model binding

The app now exposes clear extension points for all of them.
