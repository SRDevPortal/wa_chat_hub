# wa_chat_hub - Implementation Plan

## Product Goal
Create a WhatsApp Web-like ERPNext hub where agents and AI can handle customer conversations across multiple official and unofficial WhatsApp numbers, persist all messages in ERPNext, route by department, and trigger ERP workflows directly from chat.

## Delivery Strategy
- Phase 1: Core inbox foundation (official WA first)
- Phase 2: unofficial QR/session connectors
- Phase 3: AI copilot, guarded automation, workflow intelligence

---

## Phase 1 - Foundation (current build target)

### Scope
- Core doctypes
- Unified conversation model
- Desk page scaffold
- Department/account/contact/conversation/message persistence
- Basic assignment/routing service layer
- ERP action entry points

### Deliverables
1. `Chat Channel Account`
2. `Chat Contact`
3. `Chat Conversation`
4. `Chat Message`
5. `Chat Assignment Rule`
6. `Chat AI Suggestion`
7. Desk page `wa_chat_hub`
8. API/service stubs for ingestion and routing
9. Link hooks for Lead / Patient / Encounter / Support Ticket

### Exit Criteria
- One official account can be configured
- Messages can be ingested into normalized doctypes
- Conversations render in a WhatsApp-like page scaffold
- Department and assignment metadata can be stored and viewed
- Agent can trigger ERP actions from conversation context

---

## Phase 2 - Unofficial / QR Connectors

### Scope
- Session manager service
- QR onboarding lifecycle
- connector health/status model
- sync bridge into Phase 1 conversation model

### Exit Criteria
- at least one unofficial number can connect, sync, and resume reliably
- offline/reconnect state is visible in ERPNext

---

## Phase 3 - AI Copilot

### Scope
- chat summarization
- reply drafting
- intent classification
- department recommendation
- lead/ticket/encounter draft suggestions
- low-risk automation modes only

### Exit Criteria
- AI drafts are reviewable in UI
- AI actions are auditable
- no unsupervised clinical advice path exists
