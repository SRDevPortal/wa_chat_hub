# wa_chat_hub

WhatsApp Operations Hub for ERPNext / Frappe.

## Phase 1 target
- Multi-number inbox foundation
- Official WhatsApp-first normalized data model
- Conversation/message persistence in ERPNext
- Department-aware routing and assignment primitives
- WhatsApp Web-style desk page scaffold
- ERP action hooks for lead / encounter / support workflows

## Planned later phases
- Phase 2: unofficial/QR-based WhatsApp connectors
- Phase 3: AI copilot and controlled automation
# wa_chat_hub

## Customer number privacy

When privacy_shield is enabled, restricted Desk users receive masked phone fields and masked phone numbers embedded in chat text. The protection covers conversation and contact reads, notifications, AI responses, exports, diagnostics, action results, Interakt contact-sync results, and configured MCP tool responses. Raw provider payloads stay server-side, while users with full-number capability keep the original response.
