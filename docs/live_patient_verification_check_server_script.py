# Frappe Server Script
# Script Type: API
# API Method: check_patient_verification
# Allow Guest: disabled
#
# Call:
# /api/method/check_patient_verification?phone=8295269462
#
# This script is read-only. It does not verify a patient or change any record.

phone = "".join(ch for ch in str(frappe.form_dict.get("phone") or "") if ch.isdigit())
phone_last10 = phone[-10:] if len(phone) >= 10 else ""

if not phone_last10:
    frappe.throw("A valid phone number with at least 10 digits is required.")


def normalize_phone(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else ""


phone_candidates = (
    phone_last10,
    "91" + phone_last10,
    "+91" + phone_last10,
)
contacts = frappe.get_all(
    "Chat Contact",
    filters={"phone_number": ["in", phone_candidates]},
    fields=["name", "phone_number", "linked_patient"],
    limit_page_length=20,
)
matching_contacts = [
    row for row in contacts
    if normalize_phone(row.get("phone_number")) == phone_last10
]

contact_names = [row.get("name") for row in matching_contacts if row.get("name")]
conversations = []
if contact_names:
    conversations = frappe.get_all(
        "Chat Conversation",
        filters={"contact": ["in", contact_names]},
        fields=[
            "name",
            "contact",
            "channel_account",
            "linked_patient",
            "party_type",
            "identity_status",
            "agent_profile",
            "routing_reason",
            "verification_completed_at",
            "modified",
        ],
        order_by="modified desc",
        limit_page_length=50,
    )

checks = []
for conversation in conversations:
    patient = conversation.get("linked_patient")
    patient_matches = {}
    if patient and frappe.db.exists("Patient", patient):
        meta = frappe.get_meta("Patient")
        for fieldname in ("mobile", "mobile_no", "phone", "custom_whatsapp_number"):
            if meta.has_field(fieldname):
                patient_matches[fieldname] = (
                    normalize_phone(frappe.db.get_value("Patient", patient, fieldname))
                    == phone_last10
                )

    verification_agent = None
    if frappe.db.exists("DocType", "WA AI Agent Profile"):
        profile_name = frappe.db.get_value(
            "WA AI Agent Profile",
            {
                "agent_type": "Patient Verification",
                "is_active": 1,
                "is_default": 1,
            },
            "name",
        )
        if profile_name:
            profile = frappe.get_doc("WA AI Agent Profile", profile_name)
            verification_agent = {
                "name": profile.name,
                "max_tool_calls": profile.get("max_tool_calls"),
                "verify_tool_attached": any(
                    row.get("is_active")
                    and row.get("mcp_tool") == "verify_patient_identity"
                    for row in (profile.get("allowed_tools") or [])
                ),
            }

    recent_tool_events = []
    if frappe.db.exists("DocType", "MCP Event"):
        recent_tool_events = frappe.get_all(
            "MCP Event",
            filters={
                "conversation": conversation.get("name"),
                "tool_name": "verify_patient_identity",
            },
            fields=["event_time", "status", "tool_name", "error"],
            order_by="creation desc",
            limit_page_length=5,
        )

    checks.append(
        {
            "conversation": conversation,
            "patient_phone_matches": patient_matches,
            "verification_ready": bool(
                conversation.get("party_type") == "Patient"
                and conversation.get("identity_status") in ("Matched", "Verified")
                and patient
                and any(patient_matches.values())
                and verification_agent
                and verification_agent.get("verify_tool_attached")
                and int(verification_agent.get("max_tool_calls") or 0) >= 1
            ),
            "verification_agent": verification_agent,
            "recent_verification_events": recent_tool_events,
        }
    )

frappe.response["message"] = {
    "phone_last4": phone_last10[-4:],
    "matching_contacts": len(matching_contacts),
    "checks": checks,
}
