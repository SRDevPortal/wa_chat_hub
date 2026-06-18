from __future__ import annotations

import re
from dataclasses import dataclass

import frappe
from frappe.utils import formatdate

from wa_chat_hub.services import normalize_phone
from wa_chat_hub.task_logger import task_log


CLINICAL_HISTORY_RE = re.compile(
    r"\b("
    r"clinical\s+history|medical\s+history|past\s+(?:history|medication|medicine|treatment)|"
    r"case\s+history|patient\s+history|treatment\s+history|old\s+records?|previous\s+records?|"
    r"visit\s+history|consultation\s+history|encounter\s+history|doctor\s+notes?|"
    r"(?:last|latest|recent)\s+(?:details?|record|visit|consultation|appointment|prescription|treatment|medicine|medicines|dawai|dawa)|"
    r"(?:old|previous|last|purani|pichli|pehle(?:\s+wali)?)\s+"
    r"(?:medicine|medicines|medication|dawai|dawa|treatment|prescription|history|notes|record|report|visit)|"
    r"(?:medicine|medicines|medication|dawai|dawa|prescription).{0,35}"
    r"(?:history|old|previous|last|purani|pichli|pehle|di|di thi|given|prescribed|prescribe)|"
    r"(?:meri|mera|mere|my)\s+"
    r"(?:history|medicine|medicines|medication|dawai|dawa|prescription|file|record|records|notes)|"
    r"(?:kya|what|which|kaunsi|konsi).{0,45}"
    r"(?:medicine|medicines|dawai|dawa|treatment|prescription).{0,45}"
    r"(?:di|di thi|given|prescribed|prescribe|chal rahi|chalti)"
    r")\b",
    re.IGNORECASE,
)

MEDICATION_QUERY_RE = re.compile(
    r"\b(medicine|medicines|medication|meds|dawai|dawa|prescription|rx|dose|dosage|tablet|capsule|goli|syrup)\b",
    re.IGNORECASE,
)

ORDER_QUERY_RE = re.compile(
    r"\b(order|orders|items?|product|products|medicine\s+order|last\s+order|pichla\s+order|pichli\s+order)\b",
    re.IGNORECASE,
)

LAST_ONLY_RE = re.compile(
    r"\b("
    r"last|latest|recent|newest|current|"
    r"pichla|pichli|pichle|pichhle|purana|purani|pehle\s+wala|pehle\s+wali|"
    r"last\s+(?:record|details?|visit|order|prescription|medicine|appointment|consultation|follow\s*up)|"
    r"latest\s+(?:record|details?|visit|order|prescription|medicine|appointment|consultation|follow\s*up)"
    r")\b",
    re.IGNORECASE,
)

HISTORY_HINT_RE = re.compile(
    r"\b("
    r"history|record|records|file|notes|prescription|rx|treatment|consultation|consult|"
    r"visit|appointment|encounter|follow\s*up|followup|course|order|orders|"
    r"purani|purana|pichli|pichla|pehle|pehle\s+wali|last|previous|old|past|"
    r"di\s+thi|diya\s+tha|mili\s+thi|bataya\s+tha|chal\s+rahi|chalti"
    r")\b",
    re.IGNORECASE,
)

CLINICAL_DATA_RE = re.compile(
    r"\b("
    r"medicine|medicines|medication|meds|dawai|dawa|goli|tablet|capsule|syrup|dose|dosage|"
    r"order|orders|item|items|product|products|"
    r"diagnosis|problem|complaint|symptom|symptoms|investigation|test|tests|report|"
    r"doctor|dr|vaidya|notes|advice|instruction|diet|exercise|height|weight|age"
    r")\b",
    re.IGNORECASE,
)

REQUEST_HINT_RE = re.compile(
    r"\b("
    r"batao|bataye|batana|bhejo|send|share|show|dikhao|dekhna|dekh sakte|"
    r"what|which|when|kya|kaunsi|konsi|kab|kitni|details?|info|information"
    r")\b",
    re.IGNORECASE,
)

MAX_HISTORY_ROWS = 8
MAX_REPLY_ROWS = 5
MAX_NOTE_CHARS = 900


@dataclass
class ClinicalHistoryResult:
    found: bool
    reply: str
    patient: str | None = None
    patient_name: str | None = None
    encounters: list[str] | None = None
    medication_focused: bool = False
    order_focused: bool = False
    latest_only: bool = False


def is_clinical_history_query(text: str | None) -> bool:
    body = str(text or "").strip()
    if not body:
        return False
    if CLINICAL_HISTORY_RE.search(body):
        return True

    normalized = _normalize_intent_text(body)
    if not normalized:
        return False

    has_history_hint = bool(HISTORY_HINT_RE.search(normalized))
    has_clinical_data = bool(CLINICAL_DATA_RE.search(normalized))
    has_request_hint = bool(REQUEST_HINT_RE.search(normalized))

    if has_history_hint and has_clinical_data:
        return True
    if has_request_hint and has_history_hint and re.search(r"\b(meri|mera|mere|my|mujhe|me|patient)\b", normalized):
        return True
    return False


def _normalize_intent_text(text: str) -> str:
    normalized = str(text or "").lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    replacements = {
        "dawaai": "dawai",
        "dwai": "dawai",
        "dawayi": "dawai",
        "dava": "dawa",
        "davai": "dawai",
        "medicin": "medicine",
        "meds": "medicine",
        "priscription": "prescription",
        "perscription": "prescription",
        "presciption": "prescription",
        "pichle": "pichli",
        "pichley": "pichli",
        "pichhle": "pichli",
        "pichhla": "pichla",
        "purane": "purani",
        "purana": "purani",
        "pehali": "pehle",
        "pehli": "pehle",
        "phle": "pehle",
        "btana": "batana",
        "btao": "batao",
        "bta": "batao",
        "dikhao": "dikhao",
        "dikhana": "dikhao",
    }
    tokens = [replacements.get(token, token) for token in normalized.split()]
    return " ".join(tokens)


def _is_latest_only_query(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    if not normalized:
        return False
    if re.search(r"\b(all|complete|full|entire|sari|saari|sab|poori|puri|history)\b", normalized):
        return False
    return bool(LAST_ONLY_RE.search(normalized))


def build_clinical_history_reply(
    conversation: str,
    latest_text: str | None = None,
) -> ClinicalHistoryResult:
    """Resolve WhatsApp sender to Patient and answer from Patient Encounter history."""
    phone = _conversation_phone(conversation)
    normalized_query = _normalize_intent_text(str(latest_text or ""))
    medication_focused = bool(MEDICATION_QUERY_RE.search(normalized_query))
    order_focused = bool(ORDER_QUERY_RE.search(normalized_query))
    latest_only = _is_latest_only_query(normalized_query)
    task_log(
        "clinical_history",
        "matched_intent",
        conversation=conversation,
        phone=phone,
        medication_focused=1 if medication_focused else 0,
        order_focused=1 if order_focused else 0,
        latest_only=1 if latest_only else 0,
    )

    patient = _find_patient_by_phone(phone)
    if not patient:
        task_log("clinical_history", "patient_not_found", conversation=conversation, phone=phone)
        return ClinicalHistoryResult(
            found=False,
            medication_focused=medication_focused,
            order_focused=order_focused,
            latest_only=latest_only,
            reply=(
                "I could not find a patient record for this WhatsApp number. "
                "Please share your registered mobile number or patient ID."
            ),
        )

    encounters = get_patient_clinical_history(patient, limit=MAX_HISTORY_ROWS)
    patient_name = frappe.db.get_value("Patient", patient, "patient_name") or patient
    if not encounters:
        task_log("clinical_history", "history_not_found", conversation=conversation, patient=patient)
        return ClinicalHistoryResult(
            found=False,
            patient=patient,
            patient_name=patient_name,
            medication_focused=medication_focused,
            order_focused=order_focused,
            latest_only=latest_only,
            reply=(
                f"I found your patient record ({patient_name}), but no clinical history is recorded yet. "
                "Our team can check with the doctor and update you."
            ),
        )

    reply = _format_clinical_history_reply(
        patient_name=patient_name,
        encounters=encounters,
        medication_focused=medication_focused,
        order_focused=order_focused,
        latest_only=latest_only,
    )
    encounter_names = [row["name"] for row in encounters if row.get("name")]
    task_log(
        "clinical_history",
        "reply_done",
        conversation=conversation,
        patient=patient,
        encounters=",".join(encounter_names[:MAX_REPLY_ROWS]),
        medication_focused=1 if medication_focused else 0,
        order_focused=1 if order_focused else 0,
        latest_only=1 if latest_only else 0,
    )
    return ClinicalHistoryResult(
        found=True,
        patient=patient,
        patient_name=patient_name,
        encounters=encounter_names,
        medication_focused=medication_focused,
        order_focused=order_focused,
        latest_only=latest_only,
        reply=reply,
    )


def build_clinical_history_context(
    conversation: str,
    latest_text: str | None = None,
    limit: int = MAX_HISTORY_ROWS,
) -> str:
    """Return compact patient history context for the generic LLM path."""
    if not is_clinical_history_query(latest_text):
        return ""

    phone = _conversation_phone(conversation)
    patient = _find_patient_by_phone(phone)
    if not patient:
        return ""

    patient_name = frappe.db.get_value("Patient", patient, "patient_name") or patient
    encounters = get_patient_clinical_history(patient, limit=limit)
    if not encounters:
        return ""

    lines = [
        "Patient clinical history context from ERPNext Patient Encounter records:",
        f"Patient: {patient_name} ({patient})",
        "Use only these records when answering past medication/history questions. "
        "Do not invent medicines, dates, diagnoses, or instructions. "
        "Do not advise starting/stopping/changing medicines; ask the patient to confirm with the doctor.",
    ]
    for row in encounters:
        lines.append(_format_context_row(row))
    return "\n".join(line for line in lines if line)


def get_patient_clinical_history(patient: str, limit: int = MAX_HISTORY_ROWS) -> list[dict]:
    if not patient or not frappe.db.exists("DocType", "Patient Encounter"):
        return []

    meta = frappe.get_meta("Patient Encounter")
    base_fields = [
        "name",
        "encounter_date",
        "encounter_time",
        "modified",
        "status",
    ]
    optional_fields = [
        "sr_complaints",
        "sr_observations",
        "sr_investigations",
        "sr_diagnosis",
        "sr_notes",
        "sr_pe_instruction",
        "encounter_comment",
    ]
    fields = base_fields + [field for field in optional_fields if meta.has_field(field)]

    rows = frappe.get_all(
        "Patient Encounter",
        filters={"patient": patient, "docstatus": ["!=", 2]},
        fields=fields,
        order_by="encounter_date desc, encounter_time desc, modified desc",
        limit_page_length=limit,
    )

    history = []
    for row in rows:
        data = dict(row)
        data["medications"] = _encounter_medications(row.name)
        data["tests"] = _encounter_tests(row.name)
        data["order_items"] = _encounter_order_items(row.name)
        if _has_history_content(data):
            history.append(data)
    return history


def _conversation_phone(conversation: str) -> str:
    contact = frappe.db.get_value("Chat Conversation", conversation, "contact")
    if not contact:
        return ""
    return normalize_phone(frappe.db.get_value("Chat Contact", contact, "phone_number") or "")


def _find_patient_by_phone(phone: str) -> str | None:
    if not phone or not frappe.db.exists("DocType", "Patient"):
        return None

    last10 = phone[-10:] if len(phone) >= 10 else phone
    meta = frappe.get_meta("Patient")
    for fieldname in ("mobile", "phone", "mobile_no", "custom_whatsapp_number"):
        if not meta.has_field(fieldname):
            continue
        exact = frappe.db.get_value("Patient", {fieldname: phone}, "name")
        if exact:
            return exact
        rows = frappe.get_all(
            "Patient",
            filters={fieldname: ["like", f"%{last10}%"]},
            fields=["name", fieldname],
            limit_page_length=20,
        )
        for row in rows:
            normalized = normalize_phone(row.get(fieldname))
            if normalized == phone or normalized.endswith(last10):
                return row.name
    return None


def _encounter_medications(encounter: str) -> list[str]:
    medicines: list[str] = []
    for table_field in (
        "drug_prescription",
        "sr_homeopathy_drug_prescription",
        "sr_allopathy_drug_prescription",
    ):
        medicines.extend(_format_drug_rows(encounter, table_field))
    return _dedupe(medicines)


def _format_drug_rows(encounter: str, parentfield: str) -> list[str]:
    if not frappe.db.exists("DocType", "Drug Prescription"):
        return []

    meta = frappe.get_meta("Drug Prescription")
    field_candidates = [
        "medication",
        "sr_medication_name_print",
        "drug_name",
        "dosage",
        "period",
        "dosage_form",
        "sr_drug_instruction",
        "comment",
    ]
    fields = [field for field in field_candidates if meta.has_field(field)]
    if not fields:
        return []

    rows = frappe.get_all(
        "Drug Prescription",
        filters={
            "parenttype": "Patient Encounter",
            "parent": encounter,
            "parentfield": parentfield,
        },
        fields=fields,
        order_by="idx asc",
        limit_page_length=20,
    )
    formatted = []
    for row in rows:
        name = _first(row.get("sr_medication_name_print"), row.get("drug_name"), row.get("medication"))
        if not name:
            continue
        details = [
            value
            for value in (
                row.get("dosage"),
                row.get("period"),
                row.get("dosage_form"),
                row.get("sr_drug_instruction"),
                row.get("comment"),
            )
            if value
        ]
        formatted.append(f"{name} ({', '.join(details)})" if details else str(name))
    return formatted


def _encounter_tests(encounter: str) -> list[str]:
    if not frappe.db.exists("DocType", "Lab Prescription"):
        return []
    meta = frappe.get_meta("Lab Prescription")
    field_candidates = ["lab_test_name", "lab_test_code", "lab_test_comment"]
    fields = [field for field in field_candidates if meta.has_field(field)]
    if not fields:
        return []
    rows = frappe.get_all(
        "Lab Prescription",
        filters={
            "parenttype": "Patient Encounter",
            "parent": encounter,
            "parentfield": "lab_test_prescription",
        },
        fields=fields,
        order_by="idx asc",
        limit_page_length=20,
    )
    tests = []
    for row in rows:
        name = _first(row.get("lab_test_name"), row.get("lab_test_code"))
        if not name:
            continue
        comment = str(row.get("lab_test_comment") or "").strip()
        tests.append(f"{name} ({comment})" if comment else str(name))
    return _dedupe(tests)


def _encounter_order_items(encounter: str) -> list[str]:
    if not frappe.db.exists("DocType", "SR Order Item"):
        return []

    meta = frappe.get_meta("SR Order Item")
    field_candidates = [
        "sr_item_name",
        "sr_item_code",
        "sr_item_qty",
        "sr_item_uom",
        "sr_item_rate",
        "sr_item_amount",
        "sr_item_description",
    ]
    fields = [field for field in field_candidates if meta.has_field(field)]
    if not fields:
        return []

    rows = frappe.get_all(
        "SR Order Item",
        filters={
            "parenttype": "Patient Encounter",
            "parent": encounter,
            "parentfield": "sr_pe_order_items",
        },
        fields=fields,
        order_by="idx asc",
        limit_page_length=20,
    )
    items = []
    for row in rows:
        name = _first(row.get("sr_item_name"), row.get("sr_item_code"))
        if not name:
            continue
        details = []
        qty = row.get("sr_item_qty")
        uom = row.get("sr_item_uom")
        if qty:
            details.append(f"Qty {qty:g}" if isinstance(qty, float) else f"Qty {qty}")
        if uom:
            details.append(str(uom))
        if row.get("sr_item_amount"):
            details.append(f"Amount {row.get('sr_item_amount')}")
        if row.get("sr_item_description"):
            details.append(str(row.get("sr_item_description")))
        items.append(f"{name} ({', '.join(details)})" if details else str(name))
    return _dedupe(items)


def _has_history_content(row: dict) -> bool:
    text_fields = (
        "sr_complaints",
        "sr_observations",
        "sr_investigations",
        "sr_diagnosis",
        "sr_notes",
        "sr_pe_instruction",
        "encounter_comment",
    )
    return any(_clean_text(row.get(field)) for field in text_fields) or bool(
        row.get("medications") or row.get("tests") or row.get("order_items")
    )


def _format_clinical_history_reply(
    patient_name: str,
    encounters: list[dict],
    medication_focused: bool,
    order_focused: bool,
    latest_only: bool,
) -> str:
    if order_focused:
        lines = [f"As per {patient_name}'s patient encounter records, the requested order details are:"]
        reply_rows = _order_relevant_rows(encounters)
    elif medication_focused:
        lines = [f"As per {patient_name}'s patient encounter records, recent medication/history details are:"]
        reply_rows = _medication_relevant_rows(encounters)
    else:
        lines = [f"As per {patient_name}'s patient encounter records, the requested clinical details are:"]
        reply_rows = encounters

    row_limit = 1 if latest_only else MAX_REPLY_ROWS
    for row in reply_rows[:row_limit]:
        lines.extend(
            _format_reply_row(
                row,
                medication_focused=medication_focused,
                order_focused=order_focused,
            )
        )

    lines.append(
        "Please confirm with the doctor before starting, stopping, or changing any medicine."
    )
    return "\n".join(lines)


def _order_relevant_rows(encounters: list[dict]) -> list[dict]:
    scored = [(_order_relevance_score(row), idx, row) for idx, row in enumerate(encounters)]
    strong = [item for item in scored if item[0] >= 2]
    if strong:
        scored = strong
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored]


def _order_relevance_score(row: dict) -> int:
    if row.get("order_items"):
        return 3

    text = " ".join(
        _clean_text(row.get(field))
        for field in ("sr_notes", "sr_pe_instruction", "sr_complaints", "sr_diagnosis", "encounter_comment")
    ).lower()
    if not text:
        return 0
    if re.search(r"\b(order|ordered|items?|products?|delivered|medicines?\s+delivered)\b", text):
        return 2
    return 0


def _medication_relevant_rows(encounters: list[dict]) -> list[dict]:
    scored = [(_medication_relevance_score(row), idx, row) for idx, row in enumerate(encounters)]
    strong = [item for item in scored if item[0] >= 2]
    if strong:
        scored = strong
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored]


def _medication_relevance_score(row: dict) -> int:
    if row.get("medications"):
        return 3

    text = " ".join(
        _clean_text(row.get(field))
        for field in ("sr_notes", "sr_pe_instruction", "sr_complaints", "sr_diagnosis")
    ).lower()
    if not text:
        return 0

    if "draft / to be verified" in text or "current regular medications are not confirmed" in text:
        return 1
    if re.search(r"\b(medicines?\s+delivered|dosages?\s+explained|starting\s+medicines?)\b", text):
        return 2
    if re.search(r"\b(ayurvedic|homeopathy|allopathy|tablet|capsule|dose|dosage|prescribed)\b", text):
        return 2
    if MEDICATION_QUERY_RE.search(text):
        return 1
    return 0


def _format_reply_row(row: dict, medication_focused: bool, order_focused: bool) -> list[str]:
    date_text = _date_text(row.get("encounter_date"))
    header = f"\nEncounter: {row.get('name')}"
    if date_text:
        header += f" | Date: {date_text}"
    lines = [header]

    meds = row.get("medications") or []
    order_items = row.get("order_items") or []
    if order_items:
        lines.append("Order Items: " + "; ".join(order_items[:8]))
    if meds and not order_focused:
        lines.append("Medicines: " + "; ".join(meds[:8]))

    if not medication_focused and not order_focused:
        _append_field(lines, "Complaints", row.get("sr_complaints"))
        _append_field(lines, "Observations", row.get("sr_observations"))
        _append_field(lines, "Diagnosis", row.get("sr_diagnosis"))
        _append_field(lines, "Investigations", row.get("sr_investigations"))
        tests = row.get("tests") or []
        if tests:
            lines.append("Lab Tests: " + "; ".join(tests[:8]))

    notes = _clean_text(row.get("sr_notes"))
    if notes:
        lines.append(f"Notes: {_clip(notes, MAX_NOTE_CHARS)}")

    instruction = _clean_text(row.get("sr_pe_instruction"))
    if instruction:
        lines.append(f"Instruction: {_clip(instruction, 400)}")
    return lines


def _format_context_row(row: dict) -> str:
    parts = [
        f"Encounter {row.get('name')}",
        f"Date {_date_text(row.get('encounter_date'))}" if row.get("encounter_date") else "",
    ]
    for label, fieldname in (
        ("Complaints", "sr_complaints"),
        ("Observations", "sr_observations"),
        ("Diagnosis", "sr_diagnosis"),
        ("Investigations", "sr_investigations"),
        ("Notes", "sr_notes"),
        ("Instruction", "sr_pe_instruction"),
    ):
        value = _clean_text(row.get(fieldname))
        if value:
            parts.append(f"{label}: {_clip(value, 500)}")
    if row.get("medications"):
        parts.append("Medicines: " + "; ".join(row["medications"][:8]))
    if row.get("order_items"):
        parts.append("Order Items: " + "; ".join(row["order_items"][:8]))
    if row.get("tests"):
        parts.append("Lab Tests: " + "; ".join(row["tests"][:8]))
    return " | ".join(part for part in parts if part)


def _append_field(lines: list[str], label: str, value: str | None) -> None:
    cleaned = _clean_text(value)
    if cleaned:
        lines.append(f"{label}: {_clip(cleaned, 500)}")


def _date_text(value) -> str:
    if not value:
        return ""
    try:
        return formatdate(value, "dd-mm-yyyy")
    except Exception:
        return str(value)


def _clean_text(value: str | None) -> str:
    text = " ".join(str(value or "").replace("\r", "\n").split())
    return text.strip()


def _clip(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _first(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
