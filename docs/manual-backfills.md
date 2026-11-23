# Manual historical backfills

Migration and installation do not run or enqueue historical attribution, messaging
window, conversation link/identity, channel department, or phone-key backfills.
Old patch names remain registered as no-ops for upgrade compatibility. A Patch Log
entry for these retired patches does **not** mean historical data was processed.
Existing live message processing and per-document phone normalization remain enabled.

## Operator commands (not run automatically)

Run from the bench directory. Replace SITE with the actual site name.
The generic history command defaults to preview and processes at most one batch.

```sh
# Preview selected records; no history updates or cursor advancement.
bench --site SITE execute wa_chat_hub.maintenance.history_backfill.run --kwargs '{"task":"crm_lead_meta","batch_size":100,"dry_run":true}'

# Apply only when an operator explicitly decides the repair is needed.
bench --site SITE execute wa_chat_hub.maintenance.history_backfill.run --kwargs '{"task":"crm_lead_meta","batch_size":100,"dry_run":false,"after_name":""}'
```

| Task | Historical operation |
| --- | --- |
| `crm_lead_links` | Copy legacy CRM Lead/Lead references into the current lead link |
| `conversation_identities` | Reconcile legacy patient/lead identity fields and patient departments |
| `crm_lead_meta` | Fill missing CRM Lead attribution from linked, non-closed conversations |
| `messaging_windows` | Recompute conversation windows using existing inbound-message history |
| `channel_departments` | Fill missing account departments from active pipeline mappings |

If several repairs are needed, finish `crm_lead_links` before
`conversation_identities` and `crm_lead_meta`. Each task has its own cursor.
Existing identity-repair rules can replace identity/department values; review the
scope on a restored database before choosing to run that task. Preview reports
selected rows, not a field-by-field proposed diff or an exact update count.

Each successful apply returns `last_name`, `processed`, `has_more`, and `done`.
Save `last_name` and pass it as `after_name` on the next explicit invocation of the
same task and site. Cursors are operator-managed, not automatically persisted.
Stop when `done` is true. No next batch is queued. A failed batch rolls back and
raises; retry with the previous successful cursor. A `busy` result makes no
changes. Batch size is an integer from 1 to 500; JSON booleans must be real booleans.
New records inserted behind a saved name cursor require a later pass from the
beginning. Run maintenance against a stable dataset when a complete historical
snapshot is required. `processed` counts visited records, including unchanged rows.

The existing phone maintenance command remains available with its own persistent
checkpoint and verification tools:

```sh
bench --site SITE execute wa_chat_hub.maintenance.phone_backfill.backfill_phone_keys --kwargs '{"doctype":"CRM Lead","batch_size":100,"max_batches":1,"dry_run":true}'
```

Its existing apply behavior is unchanged; always supply `dry_run` explicitly.
These commands are not HTTP-whitelisted. History commands refuse migration/install
contexts. Existing AI permission checks still apply to attribution/window repairs;
explicitly disabled permission rows are not enabled by setup.

## Deployment and validation

Deploy this WA Chat Hub version together with the accompanying Shipment Tracking
change; Shipment Tracking's manual command imports the batch helper from this app.
Pause/drain workers as part of the normal maintenance procedure and restart them
with the new code before resuming jobs. Old pending messaging-window job entry
points now return `manual_backfill_required`; already-running processes with old
code cannot be stopped by changing source files alone.

On restored copies of each target database, check migration with both pending and
completed old patch logs. Verify no historical writes or backfill jobs occur, and
repeat migration to check repeatability. Check inbound chat, phone normalization
on new/edited records, and conversation loading. No manual apply commands belong
in deployment scripts. Unit regression tests mock database/queue effects; they do
not substitute for full migration tests on Bharat and SRIAAS database copies.

Index patches, index definitions, and core versions are outside this change.
