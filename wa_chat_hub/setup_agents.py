from __future__ import annotations

import json

import frappe


PATIENT_AGENT_PROMPT = """You are the clinic's Patient Care WhatsApp agent. Help verified patients with operational questions and approved care instructions using only the supplied records and knowledge. Never invent patient facts, diagnose a new condition, or change medication or dosage. If information is unavailable or clinical judgment is required, hand the conversation to the configured care team."""

VERIFICATION_AGENT_PROMPT = """You are the clinic's Patient Verification WhatsApp agent. Help with general clinic information and guide the contact through the approved identity-verification process. Do not reveal, confirm, or infer any patient record, appointment, prescription, encounter, payment, or order information until identity is verified. Escalate ambiguous or shared-number cases to a human."""

PATIENT_GUARDRAILS = """Never expose one patient's information to another contact. Do not provide diagnosis, medicine changes, dosage changes, or emergency assessment. Urgent symptoms, self-harm language, severe deterioration, adverse medicine reactions, and post-procedure complications require the approved safety response and immediate human escalation."""

PATIENT_CARE_TOOL_NAMES = (
    "get_verified_patient_profile",
    "get_verified_patient_encounters",
    "get_verified_patient_diet_charts",
    "get_verified_patient_sales_invoices",
    "get_verified_patient_doctor_certifications",
    "get_verified_patient_shipping_history",
)

PATIENT_SHIPPING_HISTORY_TOOL = {
    "tool_name": "get_verified_patient_shipping_history",
    "description": (
        "Read shipment, tracking, AWB, courier, delivery, and linked encounter shipping "
        "history for the verified patient in the current chat."
    ),
    "endpoint_url": "wa_chat_hub.mcp.patient_records.get_verified_patient_shipping_history",
    "http_method": "POST",
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


def ensure_default_agent_profiles() -> int:
    if not frappe.db.exists("DocType", "WA AI Agent Profile"):
        return 0
    specs = (
        {
            "agent_name": "Patient Care Agent",
            "agent_type": "Patient",
            "system_prompt": PATIENT_AGENT_PROMPT,
            "medical_guardrail_policy": PATIENT_GUARDRAILS,
            "escalation_policy": "Escalate when identity, record meaning, medical safety, or the requested action is uncertain.",
            "auto_reply_mode": "Draft + Approval",
            "max_tool_calls": 2,
            "require_verified_identity": 1,
        },
        {
            "agent_name": "Patient Verification Agent",
            "agent_type": "Patient Verification",
            "system_prompt": VERIFICATION_AGENT_PROMPT,
            "medical_guardrail_policy": PATIENT_GUARDRAILS,
            "escalation_policy": "Escalate identity ambiguity and shared-number cases to a human without disclosing records.",
            "auto_reply_mode": "Suggest Only",
            "max_tool_calls": 0,
            "require_verified_identity": 1,
        },
    )
    created = 0
    for spec in specs:
        if frappe.db.exists("WA AI Agent Profile", spec["agent_name"]):
            continue
        doc = frappe.get_doc(
            {
                "doctype": "WA AI Agent Profile",
                "is_active": 1,
                "is_default": 1,
                **spec,
            }
        )
        doc.insert(ignore_permissions=True)
        created += 1
    ensure_patient_care_agent_tools(commit=False)
    return created


def ensure_patient_care_agent_tools(commit: bool = True) -> dict:
    """Attach verified-patient record tools to the shared Patient Care Agent."""
    required_doctypes = ("WA AI Agent Profile", "WA AI Agent Tool", "WA MCP Tool Endpoint")
    if not all(frappe.db.exists("DocType", doctype) for doctype in required_doctypes):
        return {
            "updated": False,
            "reason": "required_doctypes_missing",
            "tools": [],
            "missing_tools": list(PATIENT_CARE_TOOL_NAMES),
        }

    if not frappe.db.exists("WA AI Agent Profile", "Patient Care Agent"):
        return {
            "updated": False,
            "reason": "patient_care_agent_missing",
            "tools": [],
            "missing_tools": list(PATIENT_CARE_TOOL_NAMES),
        }

    doc = frappe.get_doc("WA AI Agent Profile", "Patient Care Agent")
    existing_rows = {row.mcp_tool: row for row in (doc.allowed_tools or []) if row.mcp_tool}
    active_tools = set(
        frappe.get_all(
            "WA MCP Tool Endpoint",
            filters={"tool_name": ["in", PATIENT_CARE_TOOL_NAMES], "is_active": 1},
            pluck="name",
        )
    )

    changed = False
    added = []
    reactivated = []
    for tool_name in PATIENT_CARE_TOOL_NAMES:
        if tool_name not in active_tools:
            continue
        row = existing_rows.get(tool_name)
        if row:
            if not row.is_active:
                row.is_active = 1
                changed = True
                reactivated.append(tool_name)
            continue
        doc.append("allowed_tools", {"mcp_tool": tool_name, "is_active": 1})
        changed = True
        added.append(tool_name)

    if changed:
        doc.save(ignore_permissions=True)
        if commit:
            frappe.db.commit()

    configured = [
        row.mcp_tool
        for row in (doc.allowed_tools or [])
        if row.mcp_tool in PATIENT_CARE_TOOL_NAMES and row.is_active
    ]
    return {
        "updated": changed,
        "agent_profile": "Patient Care Agent",
        "tools": configured,
        "added": added,
        "reactivated": reactivated,
        "missing_tools": [
            tool_name for tool_name in PATIENT_CARE_TOOL_NAMES if tool_name not in active_tools
        ],
    }


def ensure_patient_shipping_history_tool(commit: bool = True) -> dict:
    """Create/activate shipping-history MCP and allow it in patient-care routing."""
    if not frappe.db.exists("DocType", "WA MCP Tool Endpoint"):
        return {"updated": False, "reason": "mcp_tool_endpoint_missing"}

    endpoint_updated = _upsert_shipping_history_endpoint()
    agent_result = ensure_patient_care_agent_tools(commit=False)
    department_result = _ensure_shipping_history_on_patient_departments()

    if commit:
        frappe.db.commit()

    return {
        "updated": bool(endpoint_updated or agent_result.get("updated") or department_result["updated"]),
        "tool": PATIENT_SHIPPING_HISTORY_TOOL["tool_name"],
        "endpoint_updated": endpoint_updated,
        "agent": agent_result,
        "department_profiles": department_result,
    }


def _upsert_shipping_history_endpoint() -> bool:
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
        doc = frappe.get_doc("WA MCP Tool Endpoint", tool_name)
        changed = False
        for fieldname, value in values.items():
            if doc.get(fieldname) != value:
                doc.set(fieldname, value)
                changed = True
        if not doc.is_active:
            doc.is_active = 1
            changed = True
        if changed:
            doc.save(ignore_permissions=True)
        return changed

    frappe.get_doc(
        {
            "doctype": "WA MCP Tool Endpoint",
            "is_active": 1,
            **values,
        }
    ).insert(ignore_permissions=True)
    return True


def _ensure_shipping_history_on_patient_departments() -> dict:
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
            IFNULL(linked_crm_lead, '') != ''
            OR linked_reference_doctype IN ('CRM Lead', 'Lead')
          )
        """
    )

    if frappe.db.exists("DocType", "CRM Lead") and frappe.db.has_column("CRM Lead", "sr_source_patient"):
        frappe.db.sql(
            """
            UPDATE `tabChat Conversation` c
            INNER JOIN `tabCRM Lead` l ON l.name = c.linked_crm_lead
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
        fields=["name", "tool_name", "description", "is_active"],
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


def create_kidney_department_profile() -> dict:
    """Create the initial production-safe Kidney profile without record tools."""
    medical_department = "Kidney"
    if not frappe.db.exists("Medical Department", medical_department):
        frappe.throw("Medical Department Kidney does not exist on this site.")
    if not frappe.db.exists("WA AI Agent Profile", "Patient Care Agent"):
        ensure_default_agent_profiles()

    kb_name = "Kidney Patient Operations KB"
    if not frappe.db.exists("WA AI Knowledge Base", kb_name):
        frappe.get_doc(
            {
                "doctype": "WA AI Knowledge Base",
                "kb_label": kb_name,
                "kb_type": "Department Playbook",
                "medical_department": medical_department,
                "priority": 100,
                "is_active": 1,
                "content": (
                    "KIDNEY DEPARTMENT - APPROVED OPERATIONAL STARTER CONTENT.\n\n"
                    "Scope:\n"
                    "- Help with appointment requests, callback requests, identity verification, "
                    "and routing to the Kidney care team.\n"
                    "- Use patient-specific information only after identity is Verified and only "
                    "when returned by an approved MCP tool.\n\n"
                    "Appointment requests:\n"
                    "- Collect the patient's preferred date or time and callback preference.\n"
                    "- Do not state that an appointment is booked or confirmed unless an approved "
                    "ERP tool returns confirmation.\n"
                    "- Without a booking tool, create a draft response and hand off to the appointment team.\n\n"
                    "Reports and records:\n"
                    "- Do not reveal or summarize reports in an unverified conversation.\n"
                    "- If a report or record is unavailable, do not guess; route the request to the care team.\n\n"
                    "Medicines and treatment:\n"
                    "- Never start, stop, substitute, or change a medicine or dosage.\n"
                    "- Escalate medicine, dialysis, procedure, diet, fluid-intake, and treatment-plan "
                    "questions to the Kidney clinical team unless replying with an exact, approved instruction "
                    "retrieved for the verified patient.\n\n"
                    "Safety escalation:\n"
                    "- Urgent or worsening symptoms, severe pain, breathing difficulty, confusion, "
                    "loss of consciousness, very low urine output, bleeding, severe swelling, adverse medicine "
                    "reactions, and post-procedure concerns require the approved emergency response and "
                    "immediate human escalation.\n"
                    "- Do not diagnose severity or reassure the patient that an urgent symptom is harmless.\n\n"
                    "Response style:\n"
                    "- Be concise, empathetic, and use the patient's language when possible.\n"
                    "- State clearly when the request has been handed to a human team."
                ),
            }
        ).insert(ignore_permissions=True)

    profile_name = "Kidney Patient Department Profile"
    if not frappe.db.exists("WA AI Department Profile", profile_name):
        frappe.get_doc(
            {
                "doctype": "WA AI Department Profile",
                "profile_name": profile_name,
                "agent_profile": "Patient Care Agent",
                "medical_department": medical_department,
                "is_active": 1,
                "priority": 100,
                "auto_reply_mode": "Draft + Approval",
                "prompt_overlay": (
                    "You are serving the Kidney Medical Department. Restrict responses to approved Kidney "
                    "operational knowledge and verified ERP context. Do not diagnose, interpret new symptoms "
                    "or reports, or change medicines, dialysis, procedures, diet, fluids, or treatment. "
                    "Escalate clinical uncertainty and all restricted requests to the Kidney care team."
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
                "priority",
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
        "note": "Kidney profile is Draft + Approval and has no MCP tools.",
    }
