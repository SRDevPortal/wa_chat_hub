# Mobile App chat ownership

Every Chat Channel Account whose Channel Type is exactly `Mobile App` uses
account/profile identity. No phone, country code, phone-region configuration,
or Patient record is required to open a chat. This applies to domestic and
international apps. Other channel types retain their existing phone behavior.
The historical `mobile_app_ai_account_identity`, `mobile_app_ai_phone_region`
and `mobile_app_ai_require_country_code` settings do not select or gate this path.

## Authentication and profile selection

The mobile backend must derive `external_id` from its verified login session and
call the ERP endpoints with the existing shared backend token. Never accept an
unverified account ID supplied by a client as the authenticated user.

`profile_id` is the ERP child row `name` returned in the authenticated user's
`profiles` list. With one profile, the server selects it automatically; with
multiple profiles the caller must select one. With no profile an account-level
chat opens. Unknown or another user's profile ID is rejected. Phone, email,
profile display names, and Patient IDs are not chat ownership keys.

Contacts use a unique digest of channel + ERP app user name + profile row name.
They have no routable phone. Text, attachments, AI replies, and staff replies
are pinned to the authorized conversation. Reopening a closed chat keeps the
same conversation. Existing phone-based contacts retain their naming and
mandatory-phone validation.

## Preserve profile identity during sync

Deploy the accompanying sync changes in `mobile_app` (domestic) and
`mobileintl_app` (international). When updating/reordering existing profiles,
clients must echo each profile's ERP `name` returned by lookup/sync. The server
preserves it only if it belongs to this user and appears once in the request.
New profiles omit `name`; they receive a new identity. Omitting the `profiles`
property leaves existing profiles unchanged. Sending an empty list removes
the profiles and access through those profile IDs.

A replacement profile without its existing ERP `name` is a new profile and will
not automatically inherit the old chat. The client/middleware must retain these
IDs; mapping by phone, patient ID, display name, or list position is unsafe.

## History and patient records

Phone-owned chat history is retained, but is not automatically attached to an
app account. Without independently verified ownership, the app opens a separate
conversation; authorized staff retain access to old records. Account-level chats
created without profiles remain separate from subsequently created profile chats.

A user-supplied profile `patient_id` is not proof of patient ownership. New account
chats start unlinked/unverified. Existing patient identity verification remains
necessary before patient records can be accessed. Do not migrate historical
medical chats based only on phone matches or a client-supplied Patient ID.

## Deployment and verification

Deploy the matching `wa_chat_hub` and mobile sync code, run the site's migration
(to load the Chat Contact identity fields), and restart web/worker processes.
No country-setting patch or account-mode opt-in is required. Do not push the
backend shared token into the mobile client. Review profile-ID handling in the
actual middleware before release; Flutter source is not part of these repos.

Run `wa_chat_hub.tests.test_mobile_account_identity`,
`wa_chat_hub.tests.test_mobile_app_transport`,
`wa_chat_hub.tests.test_chat_thread_continuity`, and the appropriate mobile app's
`tests.test_profile_sync_identity` with a connected test site. The account and
continuity database suites roll back fixtures and mock external providers/jobs.
Test the real mobile client against its configured backend before release.
