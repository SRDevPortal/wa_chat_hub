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

For a multi-country app, set `mobile_app_ai_require_country_code` to `1`.
This takes precedence over both the region setting and the channel's old default:
app, patient and contact phones must carry `+` or `00` country prefixes. Bare
numbers are rejected, including full international digits without a prefix.
Audit existing records and correct their verified country prefixes before enabling
this on a live multi-country site. No country is guessed and no bulk data rewrite
is performed; existing unqualified records will require correction to open chat.

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

## International email-login accounts (Mobile App only)

Deploy wa_chat_hub with its Chat Contact schema and run `bench --site SITE migrate`
before enabling `mobile_app_ai_account_identity: 1`. The international mobileintl_app
migration enables this setting; the domestic mobile_app does not. The runtime also
checks that the account Channel Type is exactly `Mobile App`. Other channel types
always retain their phone-based path, even when this site setting is enabled.

The backend resolves the authenticated Mobile App User and a stable profile child
row ID. With one profile it selects that row; with none it opens an account chat;
with multiple it requires `profile_id`. Unknown or another user's profile IDs are
rejected. The middleware must derive external_id from its verified login session.
Neither the email address nor phone number is used as a chat ownership key.

Contacts store a unique digest of channel + ERP user name + profile row ID and no
routable phone. Messages, attachments, AI/agent replies and care-team confirmations
stay on the authorized conversation. Phone validation and country defaults do not
apply to these contacts. Domestic mode remains off unless explicitly enabled.

Legacy phone-owned history is retained but not automatically adopted. A raw profile
patient_id is insufficient proof for exposing medical history: profile sync accepts
user input. This mode starts an unlinked chat; reuse of historical patient chats
requires a separately reviewed ownership migration. No Patient is required to chat.
Account chats created before a profile exists remain distinct from later profile chats.

Deploy the shared schema to both benches if both run this version. Do not disable
account mode after users start chats without planning history access; mode-off
requests intentionally cannot fall back to phone access for an account contact.
