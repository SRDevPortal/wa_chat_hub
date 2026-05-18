# WA Chat Hub Status

Last updated: 2026-05-16

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

Pending:

- Fetch approved Interakt templates.
- Show template dropdown.
- Detect required variables.
- Render preview automatically.

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

Current:

- Source details are extracted from raw message/customer payload when available.

Recommended:

- Add dedicated fields on Chat Contact or Chat Conversation for ad attribution.
- Persist Source ID, Source URL, Source, and `ctwa_clid` after first extraction.
- This will make source details stable even if old raw payloads are unavailable.

### Custom Avatar

Current:

- WhatsApp/Interakt API docs reviewed so far do not expose the customer WhatsApp profile photo/avatar.

Possible future path:

- Add `avatar_url` to Chat Contact.
- Fill from CRM Lead/Patient image, custom Interakt trait, or manually uploaded profile image.

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

## Recommended Next Tasks

1. Implement video sending.
2. Implement audio sending.
3. Test and polish received document/audio/video webhook rendering.
4. Wire search and filter chips.
5. Add right-panel assign/status/priority controls.
6. Persist Ad Source fields on contact/conversation.
7. Add chat-visible Interakt error messages.
