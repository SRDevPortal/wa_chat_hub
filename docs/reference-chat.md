# Open WhatsApp Chat from Lead and Customer

Saved ERPNext Lead and Customer forms have an **Open WhatsApp Chat** button.
Their list rows also have **Open Chat**. The existing CRM Lead form/list is supported
while the Frappe CRM migration is pending.

The WhatsApp workspace (`/app/whatsapp`) links to the actual conversation page,
`/app/wa-chat-hub`. Buttons open that page with the selected conversation ID on
the current site. They do not redirect development records into production.

Lookup uses accessible, explicitly linked conversations first, including the
original Lead associated with a Customer (and the reverse relationship). If none
exists, it matches normalized phone digits, including the country code, against
Chat Contact. Customer primary-contact numbers are included when readable.
Numbers from different countries are never equated using their last ten digits.

One match opens directly. Multiple matches show a selector with channel, status
and last-message time. Closed conversations remain available. No match displays
a message without creating a conversation. Only existing permissions allow chat
access; viewing a Lead or Customer alone does not expose inaccessible chats.

The lookup makes one browser request on click, with bounded database results.
It adds no polling, background job, message send, or conversation relinking.
Opening the existing chat page retains its normal behavior, including marking
messages read and its existing refresh mechanism.

## Validation

- Eight transactional database tests cover conversion links in both directions,
  exact phone matching, closed history, multiple matches, missing chats,
  reference permissions, ERPNext sales access and shared-document isolation.
- Three Node UI tests cover saved form buttons, routing, selection, empty results
  and coalescing repeated clicks.
- New assets return HTTP 200 on development.localhost. Site hooks were refreshed.
- No production deployment or authenticated browser test has been performed.

## Deployment

Deploy the changed WA Chat Hub source to the target site's normal app release,
build assets and clear the site's cache. Refresh Desk with Ctrl+Shift+R. No data
migration or conversation backfill is required for this feature. The Frappe Cloud
site must run this release before its buttons will appear.
