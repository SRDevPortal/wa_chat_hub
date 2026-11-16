# Automatic Patient Care routing

An incoming WhatsApp number that uniquely matches an existing Patient is linked
automatically and routed to Patient Care Agent. The conversation uses `Matched`;
it does not claim the customer completed verification. Previously verified links
remain compatible. Public messages, including greetings, stay with the care agent.
The linked Patient's registered name supplies the contact display name and a small
name-only context block for every matched-patient reply, including public greetings.
WhatsApp nicknames cannot overwrite that registered display name. Name lookups are
cached for one hour and invalidated on Patient rename/deletion; no medical record
details are loaded just to address the patient. Removing a patient link also removes
the copied patient display name. Existing contacts refresh on their next inbound.
Department settings, approved tools, intent-specific tool selection, and configured
workflows continue to apply. Patient-record endpoints still enforce that the
requested Patient is the one linked to the conversation.

Numbers without a Patient match continue through the existing customer/lead flow.
A stale Lead link cannot hide a newly created Patient. Unexpected duplicate matches
remain `Ambiguous`, without selecting a Patient or introducing a human-handoff job.

## Database and worker behavior

- Reuse the existing bounded exact queries against normalized phone indexes.
- Cache both matches and misses for five minutes. Patient creation, phone changes,
  and deletion invalidate the affected numbers before and after commit.
- Versioned cache entries prevent a late lookup from repopulating an invalidated
  result; lookup results are published only after transaction commit.
- Reuse loaded conversations; skip unchanged identity and routing writes.
- Repeated linked-patient messages skip identity reconciliation and contact-sync
  enqueueing. Verification calls, expiry checks, and verification replies are
  removed from the AI message-processing path.
- Patient/contact phone edits clear stale links. A subsequent inbound message
  resolves the current phone. Patient Encounter hooks only link a matching phone.
- No new scheduled tasks, queues, or AI calls are introduced.

## Upgrade

Run the normal site migration after deploying the code:

```sh
bench --site <site-name> migrate
```

The `automatic_patient_care` patch updates the relevant policy fields, retires
verification profiles/tools, and redirects existing matched-patient routes to the
active default patient agent. It preserves unrelated policy keys, custom care
instructions, department settings, and workflows. It is safe to rerun. Settings
are read in batches of 100; there is no conversation-table backfill. Existing chats
refresh their links and stored agent on their next inbound message. Restart the
site's workers through the normal deployment process so they load the new code.

Legacy verification functions/fields and `get_verified_patient_*` endpoint names
are retained for compatibility. Historical verification fields are hidden in the
conversation form; they are not required for automatically matched record access.

## Validation

Focused unit tests cover automatic/public routing, legacy shipment-route
compatibility, unknown/missing/ambiguous contexts, cross-patient denial, lead-link
precedence, phone-change invalidation, commit ordering, no-op writes, and migration
idempotence. Ten repeated phone lookups perform one indexed resolver call in the
cache test, including the negative-result case.

A mocked before/after comparison against the previous implementation measured an
unchanged identity reconciliation at 1 document load, 3 existence checks, and 1
write before; using the loaded conversation now takes 0 document loads, 1 existence
check, and 0 writes. The normal repeated-patient inbound path skips this helper.
These are call-count checks, not live database latency or throughput measurements.
Site-level smoke testing and load measurement must use a configured test site.

Patient greeting and identity instructions are managed in the saved Agent Profile,
Department Profile, and Policy Bundle prompts. Runtime code uses their text as
configured and supplies the registered name as JSON data only. Migrations do not
rewrite the saved patient-care prompt or policy reply templates. Configure these
prompts on each destination site when deploying; the local prompt edits are database settings.
