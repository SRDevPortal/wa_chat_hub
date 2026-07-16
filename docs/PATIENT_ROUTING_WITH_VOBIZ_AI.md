# WA Chat Hub Patient Routing With Vobiz AI

Last updated: 2026-07-09

## Purpose

When an inbound WhatsApp message comes from a known Patient, WA Chat Hub should automatically assign the chat to the correct user by using the Patient routing mappings already configured in `vobiz_ai`.

The goal is:

```text
Patient sends WhatsApp message
-> WA Chat Hub identifies the Patient by phone number
-> vobiz_ai selects the correct routing group and available routing agent
-> WA Chat Hub assigns the conversation to that agent
```

This avoids manual triage for known Patients and keeps WhatsApp chat routing aligned with the Vobiz call-routing setup.

## Current Behavior

WA Chat Hub already identifies known Patients during inbound message processing.

The main flow is in:

```text
wa_chat_hub/services.py
```

Inbound message handling:

```text
append_message()
-> _append_message_impl()
-> _link_or_create_master_record()
```

Inside `_link_or_create_master_record()`, WA Chat Hub searches for a Patient by sender phone number using these Patient fields:

```text
mobile
mobile_no
phone
custom_whatsapp_number
```

If a Patient is found, WA Chat Hub currently updates:

```text
Chat Contact.linked_patient = <patient>
Chat Contact.source_doctype = "Patient"
Chat Contact.source_name = <patient>

Chat Conversation.linked_reference_doctype = "Patient"
Chat Conversation.linked_reference_name = <patient>
```

It then queues Interakt contact sync for that conversation.

So current Patient usage is mainly:

- Link chat contact to Patient.
- Link chat conversation to Patient.
- Show Patient in WA Chat Hub sidebar.
- Support Patient chat search/filter.
- Support opening existing WhatsApp chat from Patient.
- Push Patient context/traits to Interakt.

Current behavior does not assign the conversation using `Vobiz Patient Routing Group` / `Vobiz Patient Routing Agent`.

## Desired Behavior

After WA Chat Hub identifies a known Patient, it should ask `vobiz_ai` for routing.

If routing selects an agent, update the Chat Conversation:

```text
assigned_to = selected agent_user
department = matching ERPNext Department, when available/applicable
```

If no agent is available, apply a defined fallback behavior:

```text
fallback_user -> assign conversation to fallback user
fallback department queue -> leave assigned_to empty but keep department
no fallback -> leave conversation unassigned
```

Recommended default:

```text
If selected agent_user exists:
    assign to selected agent_user
Else if fallback_user exists:
    assign to fallback_user
Else:
    leave unassigned
```

## Vobiz AI Routing Data

The routing configuration lives in `vobiz_ai`.

Primary DocTypes:

```text
Vobiz Patient Routing Group
Vobiz Patient Routing Agent
Vobiz AI Settings
```

### Vobiz Patient Routing Group

Important fields:

```text
active
display_label
sr_followup_id
medical_department
did_number
normalized_did
match_mode
strategy
priority
reserve_on_config_fetch
fallback_user
fallback_phone
fallback_message
agents
```

The group decides which team should handle a Patient.

Matching can be based on:

- Follow-up ID + Medical Department
- Follow-up ID only
- Department only
- Optional DID / incoming business number

### Vobiz Patient Routing Agent

Important fields:

```text
agent_user
agent_phone
normalized_agent_phone
enabled
availability_status
priority
weight
max_active_calls
active_call_count
today_call_count
last_patched_at
last_transfer_status
reservation_token
```

The agent row decides who inside a routing group can receive the work.

## Available Routing Agent Logic

An agent is available only if all of these are true:

```text
enabled = 1
availability_status = "Available"
agent_phone or normalized_agent_phone exists
active_call_count is below max_active_calls, when max_active_calls is set
```

Agents with these statuses are skipped:

```text
Busy
Offline
Paused
Unavailable
On Break
```

After filtering, agents are ordered by the routing group strategy.

Supported strategies:

```text
Round Robin
First Available
Least Busy
Weighted Balanced
```

The first eligible agent after sorting is selected.

For WhatsApp chat assignment, the important selected value is:

```text
selected_agent.agent_user
```

That maps to:

```text
Chat Conversation.assigned_to
```

## Proposed User Flow

1. Patient sends a WhatsApp message.
2. Interakt webhook reaches WA Chat Hub.
3. WA Chat Hub stores or updates:

```text
Chat Contact
Chat Conversation
Chat Message
```

4. WA Chat Hub identifies the Patient by phone number.
5. WA Chat Hub links the conversation to the Patient.
6. WA Chat Hub calls Vobiz Patient Routing.
7. Vobiz routing returns one of:

```text
selected agent
fallback user
no available agent
no matching routing group
patient routing disabled
```

8. WA Chat Hub updates the conversation assignment.
9. The conversation appears in the assigned user's chat queue / Mine filter.

## Proposed Technical Flow

Add one integration step inside the Patient branch of `_link_or_create_master_record()`.

Current Patient branch:

```text
patient_name = _find_by_phone(...)
if patient_name:
    update Chat Contact
    update Chat Conversation
    enqueue Interakt push
    return
```

New Patient branch:

```text
patient_name = _find_by_phone(...)
if patient_name:
    update Chat Contact
    update Chat Conversation
    apply Vobiz patient routing
    enqueue Interakt push
    return
```

Recommended helper in WA Chat Hub:

```python
def _apply_vobiz_patient_routing(conversation: str, patient: str, channel_account: str | None = None) -> None:
    ...
```

Recommended public helper in `vobiz_ai`:

```python
def resolve_patient_routing_for_chat(patient: str, did_number: str | None = None) -> dict:
    ...
```

WA Chat Hub should call the public helper, not private `_voice_agent.py` internals.

## Recommended Vobiz AI Helper Contract

Create a public helper in `vobiz_ai`, for example:

```text
vobiz_ai.api.patient_routing.resolve_patient_routing_for_chat
```

Input:

```python
{
    "patient": "PAT-0001",
    "did_number": "919220607352"  # optional business/incoming WhatsApp number
}
```

Output:

```python
{
    "success": True,
    "enabled": True,
    "matched": True,
    "status": "selected",
    "routing_basis": "Patient Routing",
    "routing_group": "VOBIZ-PAT-ROUTE-00001",
    "routing_group_label": "Followup Team A",
    "patient_routing_match_mode": "Follow-up + Department",
    "agent_row": "...",
    "agent_user": "agent@example.com",
    "agent_phone": "9999999999",
    "fallback_user": "",
    "fallback_phone": "",
    "fallback_message": ""
}
```

Possible statuses:

```text
selected
fallback
no_patient
no_route_factors
no_matching_route
no_available_agent
disabled
error
```

## WA Chat Hub Assignment Behavior

Recommended mapping:

```text
status = selected
agent_user exists
-> Chat Conversation.assigned_to = agent_user
```

```text
status = fallback
fallback_user exists
-> Chat Conversation.assigned_to = fallback_user
```

```text
status = no_available_agent
fallback_user exists
-> Chat Conversation.assigned_to = fallback_user
```

```text
status = no_matching_route / disabled / error
-> do not change assigned_to
```

If the conversation already has an assigned user, use this rule:

```text
Do not override existing assigned_to unless the message starts a new conversation or a setting explicitly allows reassignment.
```

Recommended default:

```text
Preserve existing assignment.
Only route when assigned_to is empty.
```

This prevents an active agent's conversation from being stolen by routing on every new Patient reply.

## DID / Business Number Handling

Vobiz Patient Routing Group supports optional DID matching.

For WhatsApp, the closest equivalent is the receiving business WhatsApp number from the `Chat Channel Account`.

Possible source:

```text
Chat Channel Account.phone_number
```

WA Chat Hub can pass this as `did_number` to Vobiz routing.

If no DID is available, Vobiz routing should still match groups that do not require DID.

## Permissions

WA Chat Hub inbound processing runs through the configured WA Chat Hub service user and the AI/service permission matrix.

For this feature, the service user needs read access to:

```text
Patient
Vobiz AI Settings
Vobiz Patient Routing Group
Vobiz Patient Routing Agent
User
Chat Channel Account
Chat Conversation
```

It needs write access to:

```text
Chat Conversation
```

If Vobiz routing reserves agents, it may also need write access to:

```text
Vobiz Patient Routing Agent
```

However, for WhatsApp assignment, reservation may not be required because chats are asynchronous. The recommended first version should not increment call counters unless the business explicitly wants WhatsApp chats to count against active-call capacity.

## Important Decision: Reserve Agent Or Not

The existing Vobiz voice routing may reserve an agent when config is fetched.

For calls, this makes sense:

```text
agent selected -> call transfer starts -> active_call_count increments
```

For WhatsApp chat, it may not make sense to increment call counters, because a chat can remain open for a long time.

Recommended behavior:

```text
Do not reserve agent for WhatsApp chat in version 1.
Use availability and active_call_count only as selection filters.
Set Chat Conversation.assigned_to, but do not increment active_call_count.
```

Optional later behavior:

```text
Add separate chat counters:
active_chat_count
max_active_chats
today_chat_count
```

## Audit / Traceability

Recommended fields or log entry:

Use existing `Chat Action Log` if available.

Log:

```text
conversation
patient
routing_group
routing_group_label
routing_status
selected_agent_user
fallback_user
match_mode
reason
```

This helps answer:

```text
Why was this Patient chat assigned to this user?
```

If adding fields to `Chat Conversation` is acceptable, optional fields:

```text
patient_routing_group
patient_routing_status
patient_routing_agent
patient_routing_match_mode
patient_routed_at
```

Version 1 can avoid new fields and use `Chat Action Log`.

## Edge Cases

### Patient Found But No Routing Group

Behavior:

```text
Link conversation to Patient.
Leave assigned_to unchanged.
Log status = no_matching_route.
```

### Routing Group Found But No Available Agent

Behavior:

```text
If fallback_user exists, assign to fallback_user.
Else leave unassigned.
Log status = no_available_agent.
```

### Conversation Already Assigned

Recommended behavior:

```text
Preserve existing assigned_to.
Do not reroute.
```

Reason:

Repeated inbound messages should not keep changing ownership.

### Patient Phone Matches Multiple Patients

Current WA Chat Hub `_find_by_phone()` behavior should be reviewed before implementation.

Recommended behavior:

```text
If exactly one Patient matches, route.
If multiple Patients match, do not auto-route; log ambiguous_patient_match.
```

### Patient Has No Follow-up ID Or Medical Department

Behavior depends on Vobiz settings.

Recommended:

```text
Try fallback routing if configured.
Otherwise leave unassigned.
```

### Vobiz AI App Not Installed

Behavior:

```text
Skip Vobiz routing silently or log info.
Patient linking should continue.
```

WA Chat Hub must not fail inbound message ingestion because routing is unavailable.

### Routing Error

Behavior:

```text
Catch exception.
Log error.
Do not block message storage.
Do not block Patient linking.
```

Inbound WhatsApp reliability is more important than assignment.

## Implementation Steps

### Step 1: Add Public Routing Helper In vobiz_ai

Create a stable helper that WA Chat Hub can call.

Example path:

```text
vobiz_ai/api/patient_routing.py
```

Responsibilities:

- Check `Vobiz AI Settings.enable_patient_routing`.
- Load Patient.
- Read Patient `sr_followup_id`.
- Read Patient medical department.
- Match active `Vobiz Patient Routing Group`.
- Filter available `Vobiz Patient Routing Agent`.
- Sort by strategy.
- Return selected agent/fallback/status.
- Avoid call reservation for WhatsApp chat unless explicitly requested.

### Step 2: Add WA Chat Hub Integration Helper

In:

```text
wa_chat_hub/services.py
```

Add:

```python
def _apply_vobiz_patient_routing(conversation: str, patient: str, channel_account: str | None = None) -> None:
    ...
```

Responsibilities:

- Check whether `vobiz_ai` routing helper exists.
- Pass Patient and DID/business number.
- Preserve existing assignment by default.
- Update `Chat Conversation.assigned_to` when selected/fallback user exists.
- Optionally update `department`.
- Log routing result.

### Step 3: Call Helper After Patient Link

Inside `_link_or_create_master_record()`, after:

```python
_set_conversation_fields(
    convo,
    {
        "linked_reference_doctype": "Patient",
        "linked_reference_name": patient_name,
    },
)
```

Call:

```python
_apply_vobiz_patient_routing(
    conversation=conversation,
    patient=patient_name,
    channel_account=convo.channel_account,
)
```

### Step 4: Add Tests

Recommended tests:

- Known Patient with matching group and available agent assigns conversation.
- Known Patient with existing assigned user is not reassigned.
- Known Patient with no group remains unassigned.
- Known Patient with no available agent uses fallback user.
- Vobiz AI missing/unavailable does not break inbound message.
- Non-Patient messages continue current Lead/Customer behavior.

## Expected Final Result

After implementation:

```text
Known Patient sends WhatsApp message
-> Chat is linked to Patient
-> Vobiz routing group is resolved
-> Available routing agent is selected
-> Chat Conversation.assigned_to is set
-> Agent sees the chat in their queue
```

This keeps Patient WhatsApp handling consistent with the existing Vobiz Patient Routing setup while preserving WA Chat Hub's current message ingestion and Patient linking behavior.
