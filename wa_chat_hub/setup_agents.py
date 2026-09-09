from __future__ import annotations

import json

import frappe


PATIENT_AGENT_PROMPT = """You are the clinic's Patient Care WhatsApp agent. Help verified patients with operational questions and approved care instructions using only the supplied records and knowledge. Never invent patient facts, diagnose a new condition, or change medication or dosage. If information is unavailable or clinical judgment is required, hand the conversation to the configured care team."""

VERIFICATION_AGENT_PROMPT = """You are the clinic's Patient Verification WhatsApp agent. Never verify a customer merely because their WhatsApp number matches a Patient record. Ask the customer to either provide their registered phone number or explicitly confirm ownership of the current chat number by saying words such as 'This is my number' or 'Yahi mera number hai'. Call verify_patient_identity without arguments only after the latest customer message contains one of those forms of evidence. The server validates the latest inbound message, current Chat Contact number, and linked Patient number. Confirm verification only when the tool returns verified=true. If evidence is missing, ask one concise verification question. If verification fails, disclose no patient information and escalate safely. Never reveal, confirm, or infer any patient record, appointment, prescription, encounter, payment, or order information before successful verification."""

PATIENT_GUARDRAILS = """Never expose one patient's information to another contact. Do not provide diagnosis, medicine changes, dosage changes, or emergency assessment. Urgent symptoms, self-harm language, severe deterioration, adverse medicine reactions, and post-procedure complications require the approved safety response and immediate human escalation."""

PATIENT_CARE_TOOL_NAMES = (
    "get_verified_patient_profile",
    "get_verified_patient_encounters",
    "get_verified_patient_diet_charts",
    "get_verified_patient_sales_invoices",
    "get_verified_patient_doctor_certifications",
    "get_verified_patient_shipping_history",
)

PATIENT_VERIFICATION_TOOL = {
    "tool_name": "verify_patient_identity",
    "description": (
        "Verify only when the latest customer message either supplies the matching "
        "registered phone number or explicitly claims the current WhatsApp chat number "
        "as theirs, for example 'This is my number' or 'Yahi mera number hai'."
    ),
    "endpoint_url": "wa_chat_hub.identity.verify_patient_identity_by_agent",
    "http_method": "POST",
    "access_mode": "Write",
    "parameters_schema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

PATIENT_SHIPPING_HISTORY_TOOL = {
    "tool_name": "get_verified_patient_shipping_history",
    "description": (
        "Read shipment, tracking, AWB, courier, delivery, and linked encounter shipping "
        "history for the verified patient in the current chat."
    ),
    "endpoint_url": "wa_chat_hub.mcp.patient_records.get_verified_patient_shipping_history",
    "http_method": "POST",
    "access_mode": "Read",
    "parameters_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Maximum number of recent shipping records to return.",
            }
        },
        "additionalProperties": False,
    },
}

CRM_LEAD_PROFILE_TOOL = {
    "tool_name": "get_linked_lead_profile",
    "description": (
        "Read allowlisted Lead details and notes only for the Lead linked "
        "to the current WhatsApp conversation."
    ),
    "endpoint_url": "wa_chat_hub.mcp.lead_records.get_linked_lead_profile",
    "http_method": "POST",
    "access_mode": "Read",
    "parameters_schema": {
        "type": "object",
        "properties": {
            "include_notes": {
                "type": "integer",
                "enum": [0, 1],
                "description": "Set to 1 to include recent Lead notes.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "description": "Maximum number of recent notes to return.",
            },
        },
        "additionalProperties": False,
    },
}

def ensure_default_agent_profiles() -> int:
    return 0


def ensure_verification_agent_tool(commit: bool = True) -> dict:
    """Insert the verification MCP once; never overwrite administrator edits."""
    return {"updated": False, "reason": "patient_flow_disabled"}


def ensure_patient_care_agent_tools(commit: bool = True) -> dict:
    """Attach verified-patient record tools to the shared Patient Care Agent."""
    return {
        "updated": False,
        "reason": "patient_flow_disabled",
        "tools": [],
        "missing_tools": [],
    }


def ensure_patient_shipping_history_tool(commit: bool = True) -> dict:
    """Create/activate shipping-history MCP and allow it in patient-care routing."""
    return {
        "updated": False,
        "reason": "patient_flow_disabled",
        "tool": PATIENT_SHIPPING_HISTORY_TOOL["tool_name"],
        "endpoint_updated": False,
        "agent": {},
        "department_profiles": {},
    }


def ensure_crm_lead_account_mcp_tool(commit: bool = True) -> dict:
    """Insert the Lead MCP once without changing account MCP settings."""
    if not frappe.db.exists("DocType", "WA MCP Tool Endpoint"):
        return {"updated": False, "reason": "mcp_tool_endpoint_missing"}
    endpoint_updated = _upsert_crm_lead_profile_endpoint()
    if commit:
        frappe.db.commit()
    return {
        "updated": bool(endpoint_updated),
        "tool": CRM_LEAD_PROFILE_TOOL["tool_name"],
        "endpoint_updated": endpoint_updated,
    }


def ensure_patient_draft_encounter_tool(commit: bool = True) -> dict:
    """Compatibility wrapper for the declarative insert-only AI seeder."""
    return {
        "updated": False,
        "reason": "patient_flow_disabled",
        "endpoint_updated": False,
        "seeded": {},
    }


def _upsert_shipping_history_endpoint() -> bool:
    return False
    tool_name = PATIENT_SHIPPING_HISTORY_TOOL["tool_name"]
    values = {
        key: value
        for key, value in PATIENT_SHIPPING_HISTORY_TOOL.items()
        if key != "parameters_schema"
    }
    values["parameters_schema"] = json.dumps(
        PATIENT_SHIPPING_HISTORY_TOOL["parameters_schema"],
        indent=2,
    )

    if frappe.db.exists("WA MCP Tool Endpoint", tool_name):
        return False

    frappe.get_doc(
        {
            "doctype": "WA MCP Tool Endpoint",
            "is_active": 1,
            **values,
        }
    ).insert(ignore_permissions=True)
    return True


def _upsert_crm_lead_profile_endpoint() -> bool:
    tool_name = CRM_LEAD_PROFILE_TOOL["tool_name"]
    values = {
        key: value
        for key, value in CRM_LEAD_PROFILE_TOOL.items()
        if key != "parameters_schema"
    }
    values["parameters_schema"] = json.dumps(
        CRM_LEAD_PROFILE_TOOL["parameters_schema"],
        indent=2,
    )

    if frappe.db.exists("WA MCP Tool Endpoint", tool_name):
        return False

    frappe.get_doc(
        {
            "doctype": "WA MCP Tool Endpoint",
            "is_active": 1,
            **values,
        }
    ).insert(ignore_permissions=True)
    return True


def _upsert_patient_draft_encounter_endpoint() -> bool:
    """Compatibility wrapper; endpoint values are loaded from declarative JSON."""
    return False


def _ensure_crm_lead_tool_on_account_prompt_maps() -> dict:
    if not frappe.db.exists("DocType", "WA Chat Hub Settings"):
        return {"updated": False, "reason": "settings_missing", "accounts": []}

    settings = frappe.get_single("WA Chat Hub Settings")
    tool_name = CRM_LEAD_PROFILE_TOOL["tool_name"]
    changed = False
    accounts = []

    for row in settings.get("account_prompt_maps") or []:
        if not row.get("chat_channel_account") or not row.get("is_active"):
            continue
        accounts.append(row.chat_channel_account)
        if not row.get("allow_mcp_tools"):
            row.allow_mcp_tools = 1
            changed = True
        if not row.get("max_tool_calls"):
            row.max_tool_calls = 2
            changed = True
        existing_tools = {
            part.strip()
            for part in str(row.get("mcp_tool_names") or "").replace(",", "\n").splitlines()
            if part.strip()
        }
        if tool_name not in existing_tools:
            existing_tools.add(tool_name)
            row.mcp_tool_names = "\n".join(sorted(existing_tools))
            changed = True

    if changed:
        settings.save(ignore_permissions=True)

    return {
        "updated": changed,
        "tool": tool_name,
        "accounts": accounts,
    }


def _ensure_shipping_history_on_patient_departments() -> dict:
    return {"updated": False, "reason": "patient_flow_disabled", "profiles": []}
    if not frappe.db.exists("DocType", "WA AI Department Profile"):
        return {"updated": False, "reason": "department_profile_doctype_missing", "profiles": []}

    tool_name = PATIENT_SHIPPING_HISTORY_TOOL["tool_name"]
    profiles = frappe.get_all(
        "WA AI Department Profile",
        filters={"agent_profile": "Patient Care Agent", "is_active": 1},
        pluck="name",
        limit_page_length=500,
    )

    changed = False
    added = []
    reactivated = []
    configured = []
    for profile_name in profiles:
        doc = frappe.get_doc("WA AI Department Profile", profile_name)
        existing_rows = {row.mcp_tool: row for row in (doc.allowed_tools or []) if row.mcp_tool}
        profile_changed = False
        row = existing_rows.get(tool_name)
        if row:
            if not row.is_active:
                row.is_active = 1
                profile_changed = True
                reactivated.append(profile_name)
        else:
            doc.append("allowed_tools", {"mcp_tool": tool_name, "is_active": 1})
            profile_changed = True
            added.append(profile_name)
        if profile_changed:
            changed = True
            doc.save(ignore_permissions=True)
        configured.append(profile_name)

    return {
        "updated": changed,
        "tool": tool_name,
        "profiles": configured,
        "added": added,
        "reactivated": reactivated,
    }


def backfill_conversation_identities() -> None:
    return
    if not frappe.db.exists("DocType", "Chat Conversation"):
        return
    meta = frappe.get_meta("Chat Conversation")
    required = {"linked_patient", "party_type", "identity_status"}
    if not all(meta.has_field(fieldname) for fieldname in required):
        return

    frappe.db.sql(
        """
        UPDATE `tabChat Conversation`
        SET linked_patient = linked_reference_name,
            party_type = 'Patient',
            identity_status = 'Matched',
            routing_reason = 'patient_identity:legacy_reference',
            last_identity_sync_at = NOW()
        WHERE linked_reference_doctype = 'Patient'
          AND IFNULL(linked_reference_name, '') != ''
        """
    )
    frappe.db.sql(
        """
        UPDATE `tabChat Conversation`
        SET party_type = 'Lead',
            identity_status = 'Matched',
            last_identity_sync_at = NOW()
        WHERE IFNULL(linked_patient, '') = ''
          AND (
            IFNULL(linked_lead, '') != ''
            OR linked_reference_doctype IN ('Lead')
          )
        """
    )

    if frappe.db.exists("DocType", "Lead") and frappe.db.has_column("Lead", "sr_source_patient"):
        frappe.db.sql(
            """
            UPDATE `tabChat Conversation` c
            INNER JOIN `tabLead` l ON l.name = c.linked_lead
            INNER JOIN `tabPatient` p ON p.name = l.sr_source_patient
            SET c.linked_patient = l.sr_source_patient,
                c.party_type = 'Patient',
                c.identity_status = 'Matched',
                c.linked_reference_doctype = 'Patient',
                c.linked_reference_name = l.sr_source_patient,
                c.routing_reason = 'patient_identity:crm_lead_conversion',
                c.last_identity_sync_at = NOW()
            WHERE IFNULL(l.sr_source_patient, '') != ''
              AND IFNULL(c.linked_patient, '') = ''
            """
        )

    if meta.has_field("medical_department") and frappe.db.has_column("Patient", "sr_medical_department"):
        frappe.db.sql(
            """
            UPDATE `tabChat Conversation` c
            INNER JOIN `tabPatient` p ON p.name = c.linked_patient
            SET c.medical_department = p.sr_medical_department,
                c.department_source = 'patient.sr_medical_department',
                c.department_confidence = 1.0
            WHERE IFNULL(c.linked_patient, '') != ''
              AND IFNULL(p.sr_medical_department, '') != ''
            """
        )


def get_agent_setup_status() -> dict:
    """Return a compact post-migration diagnostic for bench execute."""
    profiles = frappe.get_all(
        "WA AI Agent Profile",
        fields=[
            "name",
            "agent_type",
            "auto_reply_mode",
            "require_verified_identity",
            "max_tool_calls",
        ],
        order_by="name asc",
    )
    identity_counts = frappe.db.sql(
        """
        SELECT party_type, identity_status, COUNT(*) AS total
        FROM `tabChat Conversation`
        GROUP BY party_type, identity_status
        ORDER BY total DESC
        """,
        as_dict=True,
    )
    from wa_chat_hub.agent_router import resolve_agent_route

    sample_routes = []
    for name in frappe.get_all("Chat Conversation", pluck="name", order_by="modified desc", limit=5):
        route = resolve_agent_route(name)
        sample_routes.append(
            {
                "conversation": name,
                "party_type": route.party_type,
                "identity_status": route.identity_status,
                "agent_profile": route.agent_profile,
                "medical_department": route.department,
                "tools_available": len(route.allowed_tool_names),
            }
        )
    return {
        "profiles": profiles,
        "conversation_identity_counts": identity_counts,
        "sample_routes": sample_routes,
    }


def get_agent_route_status(conversation: str = "43") -> dict:
    """Return a JSON-safe resolved route diagnostic for a specific conversation."""
    from wa_chat_hub.agent_router import resolve_agent_route

    route = resolve_agent_route(str(conversation))
    return {
        "conversation": str(conversation),
        "party_type": route.party_type,
        "identity_status": route.identity_status,
        "agent_profile": route.agent_profile,
        "medical_department": route.department,
        "patient": route.patient,
        "routing_reason": route.routing_reason,
        "tools_available": len(route.allowed_tool_names),
        "allowed_tool_names": sorted(route.allowed_tool_names),
    }


def get_department_setup_catalog() -> dict:
    """List records that can be used to configure department agent profiles."""
    medical_departments = []
    if frappe.db.exists("DocType", "Medical Department"):
        fields = ["name"]
        meta = frappe.get_meta("Medical Department")
        for fieldname in ("department", "medical_department", "is_active", "disabled"):
            if meta.has_field(fieldname):
                fields.append(fieldname)
        medical_departments = frappe.get_all(
            "Medical Department",
            fields=fields,
            order_by="name asc",
            limit_page_length=500,
        )

    knowledge_bases = frappe.get_all(
        "WA AI Knowledge Base",
        fields=["name", "kb_label", "kb_type", "medical_department", "is_active"],
        order_by="priority desc, name asc",
        limit_page_length=500,
    )
    tools = frappe.get_all(
        "WA MCP Tool Endpoint",
        fields=["name", "tool_name", "description", "is_active", "access_mode"],
        order_by="tool_name asc",
        limit_page_length=500,
    )
    profiles = frappe.get_all(
        "WA AI Department Profile",
        fields=[
            "name",
            "profile_name",
            "agent_profile",
            "medical_department",
            "auto_reply_mode",
            "is_active",
        ],
        order_by="medical_department asc",
        limit_page_length=500,
    )
    return {
        "medical_departments": medical_departments,
        "knowledge_bases": knowledge_bases,
        "mcp_tools": tools,
        "department_profiles": profiles,
    }


def create_test_department_profile() -> dict:
    """Create an idempotent, non-sending example under the Test Medical Department."""
    medical_department = "Test"
    if not frappe.db.exists("Medical Department", medical_department):
        frappe.throw("Medical Department Test does not exist on this site.")
    if not frappe.db.exists("WA AI Agent Profile", "Patient Care Agent"):
        ensure_default_agent_profiles()

    kb_name = "Test Patient FAQ"
    if not frappe.db.exists("WA AI Knowledge Base", kb_name):
        frappe.get_doc(
            {
                "doctype": "WA AI Knowledge Base",
                "kb_label": kb_name,
                "kb_type": "FAQ",
                "medical_department": medical_department,
                "priority": 100,
                "is_active": 1,
                "content": (
                    "TEST DATA - DO NOT USE FOR PRODUCTION PATIENT CARE.\n\n"
                    "Q: How can I request an appointment?\n"
                    "A: Collect only the preferred date and callback number, then create a draft "
                    "or hand off to the appointment team. Do not claim an appointment is confirmed.\n\n"
                    "Q: Can I get my medical report on WhatsApp?\n"
                    "A: Explain that identity verification is required. Do not disclose report details "
                    "in an unverified conversation.\n\n"
                    "Q: Can the bot change my medicine or dose?\n"
                    "A: No. Escalate medicine and dosage questions to the clinical team.\n\n"
                    "Q: What happens for urgent symptoms?\n"
                    "A: Use the approved emergency response and immediately escalate to a human."
                ),
            }
        ).insert(ignore_permissions=True)

    profile_name = "Test Patient Department Profile"
    if not frappe.db.exists("WA AI Department Profile", profile_name):
        frappe.get_doc(
            {
                "doctype": "WA AI Department Profile",
                "profile_name": profile_name,
                "agent_profile": "Patient Care Agent",
                "medical_department": medical_department,
                "is_active": 1,
                "priority": 100,
                "auto_reply_mode": "Suggest Only",
                "prompt_overlay": (
                    "This is a test-only department profile. Use only approved test knowledge. "
                    "Never claim that an appointment, report delivery, medicine change, payment, "
                    "or clinical action has been completed. Always produce a suggestion for review."
                ),
                "knowledge_bases": [
                    {
                        "knowledge_base": kb_name,
                        "priority": 100,
                        "is_active": 1,
                    }
                ],
            }
        ).insert(ignore_permissions=True)

    frappe.db.commit()
    return {
        "created_or_existing": True,
        "profile": frappe.get_value(
            "WA AI Department Profile",
            profile_name,
            [
                "name",
                "agent_profile",
                "medical_department",
                "auto_reply_mode",
                "is_active",
            ],
            as_dict=True,
        ),
        "knowledge_base": frappe.get_value(
            "WA AI Knowledge Base",
            kb_name,
            ["name", "kb_type", "medical_department", "priority", "is_active"],
            as_dict=True,
        ),
        "mcp_tools": [],
        "note": "Test profile is Suggest Only and has no MCP tools.",
    }
