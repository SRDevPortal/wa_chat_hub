Policy-driven lead scoring
==========================

The scorer resolves Chat Channel Account.ai_policy_bundle for each conversation.
It does not hardcode a policy name or change the configured weights or bands.
Disabled or unassigned policies preserve existing values and do not sync zeros
over CRM scores. Stopped conversations use stopped_score (default 0) and the
configured temperature bands, without a media override.

Rules
-----

Existing base, inbound-message/unread caps, priority weights, keyword weights
and temperature bands retain their meaning. Scores remain bounded to 0..100.
The history is read in pages, including messages after the former first-40
cutoff. Optional history_limit selects the latest N messages; 0 (default) means
all history. Optional keyword_direction accepts Both (existing default), Inbound
or Outbound. No automatic change to customer-only scoring or unread weights is
made by this update.

disease_media_scoring uses its enabled flag, direction, media_types,
require_disease_related_media, disease_related_media_weight and optional
temperature_override. A qualifying actual attachment applies the configured
bonus ONCE per conversation, not once per attachment. Voice and Audio are aliases.
With require_disease_related_media=false, all matching attachments qualify,
including unrelated media: this intentionally honors the current Sriaas policy.

If require_disease_related_media=true, configure a nonempty disease_terms list.
Matching is case-insensitive literal matching against the attachment caption and
persisted OCR/transcript text, not medical diagnosis. No hardcoded disease list
is used. Pending/unavailable analysis does not qualify unless the caption matches.
Derived text is bounded to 16,000 characters per message. Analysis completion
stores it in the hidden, read-only Chat Message.scoring_media_text field and
schedules scoring again. Historical analysis stored elsewhere is not automatically
imported; strict disease matching on those old attachments needs re-analysis.

Unknown rules and invalid values fail validation instead of silently being
ignored. legacy_auto_update is a deprecated compatibility block: policy save
shows a warning and each score audit records the warning. It is never added to
the top-level weights, which would double-count legacy scores.

Synchronization and diagnostics
-------------------------------

Message inserts/edits, persisted CRM link changes, conversation input changes,
and media evidence completion enqueue scoring after commit. Separate events use
separate job IDs so an active scoring job cannot swallow a later linking event.
The conversation row is locked before calculating and writing. Existing whole-job
database lock/deadlock retries remain in place; exhausted/unrelated failures are
recorded by the Frappe background worker. Conversation and linked primary CRM
lead values are written in one transaction. Unlinked CRM leads are not given an
invented score; their existing Cold/zero display is unchanged.

Chat Conversation.lead_score_details records the policy name, version, fingerprint,
last scoring time, contributions, matching policy terms, up to 100 media message
IDs, total qualifying media count, applied override and warnings. It contains no
raw conversation or medical text. The audit is the latest calculation, not an
append-only audit history. Policy changes require explicit recalculation of old
records (or the next scoring event); they do not launch an automatic site-wide job.

Deployment and controlled backfill
---------------------------------

Deploy code, migrate the target site to add both fields, and restart its workers
before processing new jobs. No remote deployment/backfill is performed by local
implementation. Do not lower thresholds or rewrite the existing policy as part
of migration.

Preview named conversations from a site bench console::

    from wa_chat_hub.ai.lead_scoring import rescore_batch
    rescore_batch(["832953", "827467"])  # read-only; defaults to dry_run=True

After reviewing the results, explicitly queue at most 100 conversations per batch::

    rescore_batch(["832953", "827467"], dry_run=False)
    frappe.db.commit()  # makes the after-commit jobs eligible to run

Repeat in controlled batches selected by the operator. These functions are not
public HTTP endpoints and retain the existing AI DocType permission checks. They
do not send WhatsApp replies. The backfill updates linked leads only, not every
CRM record and not historical media extraction.
