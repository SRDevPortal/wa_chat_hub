# WA Chat Hub Feature Test Report

Date: 2026-05-21  
Site tested: `localhost`  
App tested: `wa_chat_hub 0.0.1` on Frappe `15.107.2`

## Summary

WA Chat Hub is installed and the main backend surfaces are loadable. Static validation passed for Python, JSON, and JavaScript. Live read-only probes confirmed an active Interakt account, existing conversations/messages, sidebar context, messaging-window state, notification count, and AI autopilot configuration.

Full automated feature coverage is not currently possible because most generated Frappe tests are placeholders, several features depend on live Interakt/WhatsApp/LLM services, and the full Frappe test runner is blocked by a non-WA Chat Hub ERPNext test fixture/site customization error.

## Automated Checks

| Check | Result | Notes |
|---|---:|---|
| Python syntax compile | PASS | `python -m compileall -q wa_chat_hub` passed using bench env Python. |
| JSON validation | PASS | All app JSON files parsed successfully. |
| JavaScript syntax | PASS | `node --check` passed for all app JS files. |
| App installed on site | PASS | `bench --site all list-apps` shows `wa_chat_hub 0.0.1 develop`. |
| Full app test suite | BLOCKED | `bench --site localhost run-tests --app wa_chat_hub` failed during ERPNext test fixture setup before useful WA Chat Hub assertions ran. |
| UI data verifier | FAIL | `wa_chat_hub.verify_ui_data.run` fails because it selects missing `CRM Note.content` column. |

## Live Probe Results

| Feature/API | Result | Evidence |
|---|---:|---|
| Channel accounts API | PASS | `get_channel_accounts` returned active `Interakt Main`, phone `919220607352`, status `Active`. |
| Conversation list API | PASS | `get_conversations` returned live conversation rows with contact, preview, unread count, lead score/language/temperature. |
| Message thread API | PASS | `get_messages(166)` returned inbound image, AI outbound reply, and status-synced outbound message. |
| Sidebar context API | PASS | `get_sidebar_context(166)` returned conversation, contact, actions, ad attribution, and messaging-window state. |
| Messaging window API | PASS | `get_messaging_window(166)` returned `free_form`, CS window, CTWA window, and expiry timestamps. |
| Runtime/MCP context API | PASS | `get_runtime_context` returned `allow_mcp_access: false`, no active tools exposed. |
| Autopilot status API | PASS | `get_autopilot_status` returned enabled, mode `Limited Auto Reply`, `sends_whatsapp: true`. |
| Notifications API | PASS | `get_unread_count` returned `77`. |
| Template API validation | PASS | `get_interakt_templates` without account/conversation rejects with validation message. |

## Feature Matrix

| Area | Feature | Status | Notes |
|---|---|---:|---|
| Core workspace | Desk page `/app/wa-chat-hub` | CODE VERIFIED | Page files and API wiring exist; browser/manual screenshot test not run. |
| Core workspace | Three-pane WhatsApp-style layout | CODE VERIFIED | Implemented in `wa_chat_hub.js` and CSS. Needs visual QA. |
| Core workspace | Contact drawer and Esc behavior | CODE VERIFIED | Implemented in JS. Needs browser QA. |
| Conversation inbox | Conversation list | PASS | API returned live data. |
| Conversation inbox | Search box | CODE VERIFIED | API and UI handlers exist. Search with non-empty query not run due command quoting issue. |
| Conversation inbox | Filter chips: All/Unread/Unassigned/Mine | CODE VERIFIED | UI filters are implemented client-side. |
| Conversation inbox | Account filter | CODE VERIFIED | UI and channel API exist. |
| Messaging | Inbound Interakt webhook | CODE VERIFIED | Handler, normalization, dedupe, and signature logic present. No synthetic POST executed. |
| Messaging | Generic legacy webhook | RISK | Guest endpoint sets Administrator and lacks equivalent signature enforcement. Disable if unused. |
| Messaging | Message persistence | PASS | Live `Chat Message` rows confirmed. |
| Messaging | Delivery/read status sync | PASS | Conversation 166 includes outbound status updated to `Read`. |
| Messaging | Realtime events | CODE VERIFIED | `publish_realtime` calls exist. Needs browser/socket QA. |
| Messaging windows | 24-hour customer service window | PASS | Live state showed active CS expiry. |
| Messaging windows | 72-hour CTWA window | PASS | Live state showed active CTWA expiry and CTWA source data. |
| Messaging windows | Free-form/template permission engine | CODE VERIFIED | Implemented in `messaging/windows.py` and outbound path. |
| Templates | Approved template fetch | CODE VERIFIED | API exists; live fetch not run to avoid external dependency/quoting issue. |
| Templates | Manual template send | EXTERNAL | Requires real Interakt call; not executed to avoid sending/charging. |
| Media | Received image rendering data | PASS | Live inbound image message with Interakt media URL and local attachment exists. |
| Media | Image upload/send | EXTERNAL | Requires file upload and Interakt API. Not executed. |
| Media | Document upload/send | EXTERNAL | Requires file upload and Interakt API. Not executed. |
| Media | Video send | PENDING | Documented pending; code has media type shape but no complete tested flow. |
| Media | Audio send | PENDING | Documented pending; no complete tested flow. |
| Contact context | Chat Contact records | PASS | Live contacts returned via conversation APIs. |
| Contact context | Sidebar contact/context | PASS | Live sidebar context returned contact and actions. |
| CRM/ERP actions | Create Lead / Issue / Patient links | CODE VERIFIED | API handlers exist. Not executed to avoid creating records. |
| CRM/ERP references | Open chat from CRM/Patient/Encounter | CODE VERIFIED | JS hook and backend reference APIs exist. Needs UI QA. |
| Assignment | Assigned user/status/priority fields | CODE VERIFIED | Doctype fields and bulk APIs exist. |
| Assignment | Bulk assign/update | CODE VERIFIED | Permission-checked APIs exist. Not executed to avoid modifying records. |
| Ad attribution | CTWA/ad source extraction/persistence | PASS | Live conversation has `source_id`, `source_url`, and `ctwa_clid`. |
| Navbar | WhatsApp unread badge/dropdown | CODE VERIFIED | JS and notifications API exist; browser QA not run. |
| AI | AI summary/draft | CODE VERIFIED | APIs exist. Not executed against LLM. |
| AI | Autopilot status/config | PASS | Enabled in Limited Auto Reply mode. |
| AI | Autopilot live behavior | PASS | Conversation 166 shows inbound image followed by AI outbound reply through Interakt. |
| AI | Duplicate prevention/window skip | CODE VERIFIED | Logic present in `api/ai_bot.py`; no destructive live test run. |
| AI | Multilingual policy | CODE VERIFIED | Prompt/settings logic exists. Needs controlled conversation tests. |
| Knowledge base | Keyword KB search | CODE VERIFIED | Implementation exists; live result not captured. |
| MCP/tools | Runtime context | PASS | MCP disabled on tested site, no tools exposed. |
| MCP/tools | Tool execution | RISK | Powerful endpoint exists. Needs role checks, allowlists, audit logging before enabling. |
| Interakt admin | Channel account config | PASS | Active Interakt account found. |
| Interakt admin | Contact sync helpers | CODE VERIFIED | APIs/helpers exist. Not executed to avoid external writes. |
| Diagnostics | Inbound/autopilot/template scripts | CODE VERIFIED | Scripts exist. `verify_ui_data` currently fails on CRM Note schema. |
| Docs | `FEATURES.md` | NEEDS CLEANUP | Useful, but contains mojibake artifacts and duplicate AI Autopilot section. |

## Failures And Blockers

1. Full Frappe tests are blocked by ERPNext fixture setup.
   - Command: `bench --site localhost run-tests --app wa_chat_hub`
   - Failure: ERPNext United States fixture setup fails with `Title field must be a valid fieldname`, then `Failed to setup defaults for country United States`.
   - Impact: prevents using the normal app test suite as proof.

2. `verify_ui_data.run` fails against current CRM schema.
   - Command: `bench --site localhost execute wa_chat_hub.verify_ui_data.run`
   - Failure: `Unknown column 'content' in 'SELECT'` for `CRM Note`.
   - Likely fix: inspect CRM Note meta and select only fields that exist, probably `note` on this site.

3. Existing test files are mostly placeholders.
   - Examples: `test_chat_message.py`, `test_chat_conversation.py`, `test_chat_contact.py`, `test_chat_channel_account.py`, `test_wa_llm_provider.py`.
   - Impact: even if the test runner worked, coverage would remain thin.

4. External integration tests were not executed.
   - Sending text/templates/media can send real WhatsApp messages through Interakt.
   - LLM tests can consume API credits and produce live replies because autopilot is enabled.

## Recommended Test Plan

1. Fix the site/test-runner blocker so `bench --site localhost run-tests --app wa_chat_hub` can run cleanly.
2. Add real unit tests for:
   - Interakt webhook normalization and dedupe.
   - Messaging-window transitions and send permission.
   - Outbound payload generation for text/template/image/document.
   - Permission checks on whitelisted APIs.
   - Autopilot duplicate prevention and closed-window skip.
3. Add a controlled staging Interakt account and test phone number for:
   - Text send.
   - Template send outside free-form window.
   - Image/document upload and send.
   - Delivery/read webhook status updates.
4. Add Playwright/browser QA for:
   - Main chat workspace layout.
   - Search/filter chips/account filter.
   - Composer disabled/enabled states.
   - Media viewer.
   - Navbar unread badge.
5. Harden before production:
   - Remove broad `All` write permissions on chat doctypes.
   - Add permission checks to all conversation read/send APIs.
   - Lock down MCP tool execution with allowlists, roles, and audit logs.
   - Disable or secure the legacy generic webhook endpoint.
