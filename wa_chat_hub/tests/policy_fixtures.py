from wa_chat_hub.policy import PolicyBundle


IDENTITY_POLICY = {
    "phone_fields": {"Patient": ["mobile", "phone"]},
    "phone_candidate_min_digits": 10,
    "phone_match_last_digits": 10,
    "allow_supplied_registered_number": True,
    "allow_current_chat_number_claim": True,
    "verification_tool_name": "verify_patient_identity",
    "current_number_claim_phrases": {
        "en": [
            "this is my number",
            "this is my whatsapp number",
            "current number is mine",
        ],
        "hi-latn": ["yahi mera number hai", "ye mera number h", "ye mera no h"],
    },
    "validity_hours": 24,
    "statuses": {
        "verified": "Verified",
        "matched": "Matched",
        "unverified": "Unverified",
        "ambiguous": "Ambiguous",
    },
}

LANGUAGE_POLICY = {
    "default_language": "en",
    "default_language_label": "English",
    "script_ratio_threshold": 0.15,
    "roman_marker_ratio_threshold": 0.12,
    "roman_ascii_ratio_threshold": 0.5,
    "ascii_ratio_threshold": 0.6,
    "roman_language_code": "hi-latn",
    "roman_language_label": "Hindi (Hinglish / Roman)",
    "ascii_language_code": "en",
    "ascii_language_label": "English",
    "script_ranges": [
        {"code": "ta", "label": "Tamil", "start": "0B80", "end": "0BFF"},
    ],
    "roman_markers": ["meri", "mera", "batao"],
}

TEST_POLICY = PolicyBundle(
    name="Test Policy",
    version=1,
    sections={
        "identity_policy": IDENTITY_POLICY,
        "party_routing_policy": {
            "allowed_party_types": ["Unknown", "Lead", "Patient", "Customer"],
            "unknown_party_type": "Unknown",
            "party_type_by_reference_doctype": {
                "Patient": "Patient",
                "CRM Lead": "Lead",
                "Lead": "Lead",
                "Customer": "Customer",
            },
            "ambiguous_patient_party_type": "Patient",
            "agent_type_by_party_status": {
                "Patient": {"Verified": "Patient", "default": "Patient Verification"},
                "Lead": {"default": "General"},
                "Unknown": {"default": "General"},
            },
        },
        "language_policy": LANGUAGE_POLICY,
        "reply_templates": {
            "system_identity_verification": {
                "default": "Do not automatically verify. Accept a registered number or 'This is my number', then call verify_patient_identity without arguments."
            },
            "system_identity_verified": {
                "default": "Identity verified; briefly confirm success and continue the most recent unresolved request."
            },
            "system_public_patient_conversation": {
                "default": "This is public context; do not ask for verification."
            },
        },
        "patient_creation_policy": {
            "create_if_missing": True,
            "missing_required_field_action": "stop",
            "required_patient_fields": ["first_name", "mobile", "company"],
            "demographic_defaults": {},
        },
        "runtime_policy": {"maximum_tool_calls": 5},
    },
)
