# Mobile app chat identity

Mobile chat normalizes the app user, patient contact, conversation lookup and
ownership check to the same international phone identity. Existing contacts and
messages are retained; there is no bulk rewrite or deletion of phone records.

Numbers with `+` or `00` carry their own country code. For numbers stored locally
without a country code, set the site's `mobile_app_ai_phone_region` to its ISO
two-letter region (for example `IN`, `US`, or `GB`). Without this setting, mobile
chat uses the channel account's existing `interakt_default_country_code`.
Check that fallback on international sites: older accounts may have inherited
the `+91` default. Do not guess a country from the account's display name.
New mobile contacts store the explicit `+` country prefix. Legacy international
digits and local aliases in the configured country are reused when unambiguous.

A selected patient must come from the authenticated Mobile App User's stored
profile list. That link permits the patient's phone to differ from the login
phone. A patient inferred only by phone lookup must still pass the phone check.
Last-ten-digit indexes identify candidates only; the actual patient phone is
checked before using the match. Other accounts and other patients remain denied.

Patients sharing a phone retain separate mobile conversations once linked.
Unlinked pre-profile history can be reused, but a conversation already linked
to another patient is never reassigned by opening a profile. Text and attachments
are pinned to the conversation that passed authorization.

## Before deploying to each live site

1. Compare the affected Mobile App User phone, selected profile's patient link,
   Patient phone and conversation's Chat Contact phone using read-only inspection.
2. Verify the region setting against the actual local-number data. Set `IN` for
   Indian national numbers and `US` for US national numbers; mixed-country sites
   should store explicit country codes. Do not match users by phone suffix.
3. Deploy the same backend revision to both sites and restart their workers/web
   processes. The API request and response shapes are unchanged.
4. Check opening existing chats, family profiles, text and media sending, and
   rejection of unrelated profiles and other app channels.

The Flutter error presentation is a separate client change. These backend edits
do not replace Flutter's raw-traceback display, and no Flutter source is present
in this workspace. Live record corrections require verified affected records;
this change does not automatically rewrite them.

## Tests

`python -m unittest wa_chat_hub.tests.test_mobile_app_identity` runs without a site.
Run `wa_chat_hub.tests.test_mobile_app_identity_database` and
`wa_chat_hub.tests.test_chat_thread_continuity` with a connected local test site.
The database tests roll back fixtures and suppress jobs and external providers.
