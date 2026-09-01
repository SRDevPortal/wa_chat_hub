# Patient Agent and Department Profiles

WA Chat Hub uses one shared Patient Care Agent with department-specific profiles. A department profile changes only its approved knowledge, prompt overlay, MCP tools, escalation destination, and optional reply mode.

## Safe defaults

- **Patient Verification Agent**: Suggest Only, no tools.
- **Patient Care Agent**: Draft + Approval, maximum two tool calls.
- Patient MCP tools are unavailable until `Chat Conversation.identity_status` is `Verified`.
- A lead-to-patient match becomes `Matched`, never automatically `Verified`.
- Existing lead relationships are retained for attribution after a patient is linked.

## Create a department profile

1. Open **WA AI Department Profile**.
2. Create a profile such as `Neurology Patient Profile`.
3. Select `Patient Care Agent` and the clinical Medical Department.
4. Add a short department prompt overlay. Do not duplicate shared medical safety rules.
5. Add only approved department knowledge bases.
6. Add only the MCP tools required by that department.
7. Leave Auto Reply Mode empty to inherit `Draft + Approval` during rollout.

Knowledge lookup is restricted to the configured profile when explicit knowledge bases are selected. Without an explicit list, the existing department and channel filters remain in effect.

## MCP requirements

Each patient MCP endpoint must:

- accept the server-injected `patient` argument;
- ignore or reject caller-supplied patient identifiers;
- return a small structured JSON response;
- expose only fields approved for WhatsApp use;
- be read-only during the first rollout;
- fail closed when identity or ERP data is unavailable.

Enabling global MCP access alone no longer exposes all tools. A tool must also be present on the routed agent or department profile.

## Identity verification

The application currently provides an administrative bridge method:

`wa_chat_hub.api.agent_routing.mark_patient_identity_verified`

Only a System Manager can invoke it, and the supplied patient must match the conversation. Connect the approved OTP workflow to this method server-side before enabling patient auto-replies. Verification can be revoked with:

`wa_chat_hub.api.agent_routing.revoke_patient_identity_verification`

Do not mark bulk conversations verified based only on a matching phone number; shared family numbers must be handled as ambiguous.

## Rollout

1. Configure department profiles and read-only tools.
2. Keep the Patient Care Agent in Draft + Approval.
3. Run shadow/approval review per department.
4. Add the approved OTP workflow.
5. Enable Limited Auto Reply only for selected low-risk departments/intents after review.
