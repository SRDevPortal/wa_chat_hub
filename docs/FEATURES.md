# WA Chat Hub Status

Last updated: 2026-05-20

WA Chat Hub is a Frappe Desk app for handling Interakt WhatsApp conversations inside ERP/Frappe. It provides a WhatsApp-style inbox, realtime message updates, media sending, assignment support, CRM actions, and contact context.

## Done

### Main Chat Workspace

Route:

```text
/app/wa-chat-hub
```

- WhatsApp-style three-pane workspace.
- Frappe page header is hidden for a cleaner app layout.
- Left conversation pane widened and restyled like WhatsApp.
- Center chat pane with message thread and fixed bottom composer.
- Right contact info drawer is hidden by default and opens from the conversation header/profile area.
- `Esc` closes the media viewer, then right drawer, then the selected chat.
- Empty state is shown when no conversation is selected.

### Conversation Inbox

- Conversation list shows avatar, contact name, message preview, time, and unread badge.
- Left title is `WA Chat Hub`.
- Search box and filter chips are present.
- Image previews show an image icon and `Photo` when no real caption exists.
- Document previews show a document icon and the caption/file context instead of raw `[Document]` text.
- Generic media placeholder text is cleaned both in backend preview generation and frontend rendering.

### Message Composer

- WhatsApp-like rounded composer.
- Attach plus icon updated to WhatsApp-style SVG.
- Emoji/template icon updated to WhatsApp-style SVG.
- Enter sends the message.
- Shift + Enter inserts a new line.
- Composer grows for multiline text and then scrolls internally.
- Send button is attached to the composer like WhatsApp.

### Interakt Text Sending

- Uses Interakt only. Wabu was reviewed only as an API structure reference.
- Normal/session text messages send through Interakt public message API.
- Working payload shape:

```json
{
  "countryCode": "+91",
  "phoneNumber": "9667465966",
  "type": "Text",
  "data": {
    "message": "hello"
  }
}
```

- Works during the WhatsApp 24-hour customer service window.
- Outbound messages sent from WA Chat Hub are saved locally immediately.

### Messaging Windows (Meta / Interakt Rules)

WA Chat Hub tracks and enforces WhatsApp messaging windows per **Chat Conversation** (strict server-side + UI).

| Rule | Behavior in WA Chat Hub |
|------|-------------------------|
| **24-hour customer service** | Each inbound customer message sets `last_customer_message_at` and `customer_service_window_expires_at` (+24h). Resets on every customer reply. |
| **72-hour CTWA ad-entry** | First inbound with CTWA/referral (`ctwa_clid`, click-to-WhatsApp payload) sets `ctwa_entry_at` and `ctwa_window_expires_at` (+72h). |
| **Free-form send** | Text/media allowed only while CS or CTWA window is active (`evaluate_send_permission` in [`outbound.py`](../wa_chat_hub/outbound.py)). |
| **Template send** | Always allowed. Updates `last_template_sent_at` / `last_template_category`. Does **not** open free-form; customer must reply. |
| **AI Autopilot** | Skips auto-reply when free-form window is closed (logs `WA AI Autopilot Skipped`). |

**Chat Conversation fields:** `messaging_window_mode`, `last_customer_message_at`, `customer_service_window_expires_at`, `ctwa_entry_at`, `ctwa_window_expires_at`, `last_template_sent_at`, `last_template_category`, `source_id`, `source_url`, `source`, `ctwa_clid`.

**Engine:** [`wa_chat_hub/messaging/windows.py`](../wa_chat_hub/messaging/windows.py)

**Webhooks to listen to (Interakt):**

- `message_received` — opens/resets customer service window; may set CTWA 72h window
- Status events (`message_api_delivered`, etc.) — delivery ticks only

**API / realtime:**

- `get_sidebar_context` includes `messaging_window`
- `get_messaging_window(conversation)` for CRM/automation
- Realtime: `wa_chat_window_updated`

**UI:** Composer banner (green = free messaging until expiry; orange = template required). Composer/attach disabled when window closed; Template button emphasized.

**Migrate:** `after_migrate` runs `backfill_messaging_windows_from_history()` from existing Chat Messages.

Template approval timing (minutes to 24h) remains in **Interakt dashboard**, not ERP.

### Inbound Webhooks

Endpoint:

```text
/api/method/wa_chat_hub.api.webhook.receive_interakt
```

- Receives customer messages from Interakt.
- Creates or updates Chat Contact, Chat Conversation, and Chat Message.
- Deduplicates repeated Interakt message IDs.
- Stores raw webhook payload for debugging and source extraction.
- Customer messages are saved as inbound/received.

### Realtime Updates

Frappe realtime events:

- `wa_chat_new_message`
- `wa_chat_message_status_updated`
- `wa_chat_conversation_updated`

Implemented behavior:

- New messages appear without page refresh.
- Current thread refreshes when selected conversation receives a message.
- Message delivery/read status updates instantly.
- Conversation unread counts refresh.
- Navbar unread badge refreshes.

### Message Status UI

- Outbound messages show WhatsApp-like time and tick indicators.
- Read/delivered status updates from Interakt webhooks.
- Status text is no longer the primary visual surface for normal message bubbles.

### Image Sending And Viewing

- Agent can upload a local image file.
- App uploads the file to Interakt and uses Interakt's hosted media URL.
- Image sends through Interakt as `type: "Image"`.
- Image preview renders inside the chat bubble.
- Captions are supported.
- Generic captions like `[Image message received]`, `Image message received`, `Photo`, and `None` are suppressed in the message bubble.
- Clicking image opens an in-app media viewer instead of directly opening the URL.
- Media viewer supports:
  - WhatsApp-like topbar SVG icons
  - Close
  - Download
  - Zoom in/out buttons
  - Mouse-wheel zoom
  - Pointer cursor on hover

### Document Sending

- Agent can upload a local document file.
- App uploads the file to Interakt and uses Interakt's hosted media URL.
- Document sends through Interakt as `type: "Document"`.
- Caption/message is supported.
- File name is stored and displayed.
- File size is stored and displayed.
- PDF documents render with a WhatsApp-like PDF card.
- Document card shows:
  - PDF/document icon
  - File name
  - File type and size
  - Caption, when provided
  - Time and tick status

### Media Receiving

- Incoming Interakt media URLs are stored locally.
- Image media renders inline.
- Document/image previews are cleaned for conversation list display.
- Existing Interakt SAS URLs are preserved safely so Azure signatures are not broken by double encoding.

### Contact Info Drawer

- Right pane opens smoothly from the right.
- Right pane is independently scrollable.
- Header uses WhatsApp-like `X` close icon and `Contact info` title.
- Shows avatar initials, name, phone, lead, patient, assigned user, and department.
- Phone numbers display with country code formatting.
- ERP actions remain available:
  - Create Lead
  - Create Support Ticket
- AI actions remain available:
  - AI Summary
  - AI Draft

### Ad Source / CTWA Details

- Ad source details are extracted from Interakt customer traits/raw payload.
- The Source section is shown only after clicking `Show More`.
- Source section is labeled `Ad Source`.
- Supported fields:
  - Source ID
  - Source URL
  - Source
  - `ctwa_clid`
- Source value now follows Interakt naming, for example `Whatsapp`, instead of showing internal values like `ad`.

### Navbar WhatsApp Widget

- Top navbar WhatsApp icon shows unread count.
- Dropdown shows recent inbound WhatsApp messages.
- Realtime events update the badge.
- `Open Hub` jumps to WA Chat Hub.

### Chat Conversation List View

Route:

```text
/app/chat-conversation
```

- Standard Frappe list view exists for queue-style work.
- Useful fields are visible for assignment/review.
- Bulk assignment actions exist.
- `List View` navigation from WA Chat Hub exists.

### Assignment Foundation

- Conversations have assignment-related fields.
- Bulk assignment from list view is available.
- Frappe assignment records are created/cleared during assignment changes.

## Partially Done

### Template Messages

Done:

- Template button/dialog exists.
- Manual template sending path exists.

Done:

- Fetches approved Interakt templates via API (cached 5 min per channel account).
- Template dialog shows dropdown of approved templates (no manual template name entry).
- Language code and body preview auto-fill from selected template.
- Variable count hints on body/header fields.
- Uses Interakt v2 API: `https://api.interakt.ai/v1/organizations/{org_id}/message-templates/v2/`
- Set **Interakt Organization ID** on Chat Channel Account, or paste the full Templates API URL.

### Search And Filters

Done:

- Search UI exists.
- Filter chips exist: `All`, `Unread`, `Unassigned`, `Mine`.

Pending:

- Wire search fully by phone/name/lead/patient.
- Wire filter chips to real query behavior.

### Error Visibility

Done:

- Failed sends are stored in raw payload/error logs.

Pending:

- Show clear Interakt failure reason directly inside the chat thread.

## Pending

### Video Sending

- Upload local video to Interakt.
- Send as `type: "Video"`.
- Render video preview/player in chat.
- Show caption, time, and status.

### Audio Sending

- Upload local audio to Interakt.
- Send as `type: "Audio"`.
- Render audio player in chat.
- Show caption/time/status where applicable.

### Received Document / Audio / Video Polish

- Received images are usable now.
- Received document/audio/video rendering should be tested and polished against real Interakt webhook payloads.
- Add consistent cards for received documents and native players for received audio/video.

### Right Panel Queue Controls

- Add quick controls for:
  - Assign to user
  - Status
  - Priority
  - Department
- Keep list view bulk actions for larger queue work.

### Ad Source Persistence

Done on **Chat Conversation**:

- `source_id`, `source_url`, `source`, `ctwa_clid` persisted on first CTWA/referral inbound (see Messaging Windows).
- Sidebar still shows Ad Source from conversation fields + latest payload fallback.

### Custom Avatar

Current:

- WhatsApp/Interakt API docs reviewed so far do not expose the customer WhatsApp profile photo/avatar.

Possible future path:

- Add `avatar_url` to Chat Contact.
- Fill from CRM Lead/Patient image, custom Interakt trait, or manually uploaded profile image.

### AI Autopilot (WhatsApp auto-reply)

Setup:

1. **WA Chat Hub Settings** — enable **Enable AI Autopilot**, set **Autopilot Mode** to `Limited Auto Reply`, configure **System Prompt** and guardrails.
2. **WA LLM Provider** — active OpenAI (or compatible) provider with API key and model (e.g. `gpt-4o-mini`).
3. **WA Knowledge Base** (optional) — Active entries with embeddings for RAG context.
4. **Chat Channel Account** — Interakt API key and webhook configured.
5. **Background worker** — `bench worker --queue short,default,long` must be running (autopilot uses the `short` queue).

Quick setup command:

```bash
bench --site localhost execute wa_chat_hub.setup_autopilot.run
bench restart
```

Behavior:

- Inbound text message → background job → LLM reply → sent via Interakt → saved as outbound `Chat Message` with `sender_type: AI`.
- `Suggest Only` / `Draft + Approval` modes create **Chat AI Suggestion** only (no WhatsApp send).
- Skips empty inbound bodies, closed conversations, and duplicate AI replies within 30 seconds.
- **Stop / start:** magic-wand icon (left header) or **AI On / AI Off** pill in the thread header toggles **Enable AI Autopilot** instantly.
- **Multilingual:** auto-replies match the customer's language (Hindi, Hinglish, English, Tamil, Telugu, Bengali, Gujarati, Punjabi, Marathi, Kannada, Malayalam, Urdu, etc.). Toggle **Enable Multilingual Auto-Replies** in **WA Chat Hub Settings**.

### Interakt Manual Inbox Sync

Current:

- Messages sent through this app sync.
- Customer inbound messages sync.
- Interakt delivery/read status webhooks sync.

Pending:

- Messages manually sent from the Interakt inbox may not appear in WA Chat Hub unless Interakt provides a webhook or conversation history API for those messages.

Required from Interakt:

- Agent/manual inbox sent-message webhook, or
- Conversation history/messages API.

### Admin Diagnostics

- Add page/report for:
  - Failed webhook payloads
  - Failed outbound sends
  - Missing Interakt config
  - Recent API errors

### AI Autopilot (auto-reply on WhatsApp)

Setup (one-time):

```bash
bench --site <site> execute wa_chat_hub.setup_autopilot.run
```

Configure:

- **WA Chat Hub Settings**: Enable AI Autopilot, Autopilot Mode = `Limited Auto Reply`, system prompt + guardrails.
- **WA LLM Provider**: Active OpenAI (or Gemini/Custom) provider with API key and model name.
- **WA Knowledge Base** (optional): Active entries with embeddings for RAG context.
- **Workers**: `bench worker --queue short,default,long` must be running (autopilot uses the `short` queue).

Flow:

1. Inbound `Chat Message` from Interakt webhook triggers `wa_chat_hub.api.ai_bot.on_message_received`.
2. Background job calls LLM with conversation history + knowledge-base context.
3. When Autopilot Mode is `Limited Auto Reply`, reply is sent via Interakt and saved as outbound `sender_type: AI`.
4. Other modes (`Suggest Only`, `Draft + Approval`) create a **Chat AI Suggestion** only.

## Recommended Next Tasks

1. Implement video sending.
2. Implement audio sending.
3. Test and polish received document/audio/video webhook rendering.
4. Wire search and filter chips.
5. Add right-panel assign/status/priority controls.
6. Persist Ad Source fields on contact/conversation.
7. Add chat-visible Interakt error messages.
