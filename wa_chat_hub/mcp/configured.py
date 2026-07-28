from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint, getdate, nowdate, nowtime

from wa_chat_hub.security import assert_ai_doctype_permission, safe_ai_get_doc


def execute_configured_tool(__mcp_tool_name: str | None = None, **kwargs) -> dict[str, Any]:
    """Execute an MCP tool whose behavior is configured on WA MCP Tool Endpoint.

    This handler intentionally keeps business behavior in the endpoint's
    Execution Config JSON. The code here is a reusable, permission-checked
    executor for creating Frappe documents from chat context and tool args.
    """
    tool_name = str(__mcp_tool_name or "").strip()
    if not tool_name:
        frappe.throw(_("MCP tool context is required."))

    endpoint = safe_ai_get_doc("WA MCP Tool Endpoint", tool_name)
    config = _load_config(endpoint.get("execution_config"))
    action = str(config.get("action") or "").strip() or "insert_doc"
    if action != "insert_doc":
        frappe.throw(_("Unsupported configured MCP action: {0}").format(action))

    confirmation_field = str(config.get("requires_confirmation_field") or "").strip()
    if confirmation_field and not cint(kwargs.get(confirmation_field)):
        frappe.throw(_("Customer confirmation is required before running this MCP."))

    target_doctype = str(config.get("target_doctype") or "").strip()
    if not target_doctype:
        frappe.throw(_("target_doctype is required in MCP Execution Config."))

    assert_ai_doctype_permission(target_doctype, "write")

    context: dict[str, Any] = {
        "tool_name": tool_name,
        "today": nowdate(),
        "nowtime": nowtime(),
    }

    patient_config = config.get("resolve_patient")
    if isinstance(patient_config, dict) and patient_config.get("enabled"):
        patient_name = _resolve_or_create_patient_from_chat(patient_config, kwargs)
        patient_doc = safe_ai_get_doc("Patient", patient_name)
        context["patient"] = patient_name
        context["patient_doc"] = patient_doc
        context["company"] = _resolve_company(patient_config.get("company"), kwargs)

    field_values = config.get("field_values")
    if not isinstance(field_values, dict):
        frappe.throw(_("field_values must be an object in MCP Execution Config."))

    doc = frappe.new_doc(target_doctype)
    for fieldname, spec in field_values.items():
        value = _resolve_value(spec, kwargs, context)
        _set_if(doc, fieldname, value)

    doc.insert(
        ignore_permissions=True,
        ignore_links=bool(config.get("ignore_links", True)),
    )

    response = {
        "success": True,
        "doctype": target_doctype,
        "name": doc.name,
        "message": "{0} created.".format(target_doctype),
    }
    for fieldname in config.get("return_fields") or []:
        if doc.meta.has_field(fieldname):
            response[fieldname] = doc.get(fieldname)
    if context.get("patient"):
        response["patient"] = context["patient"]
    return response


def _resolve_or_create_patient_from_chat(config: dict[str, Any], args: dict[str, Any]) -> str:
    conversation = str(args.get("conversation") or "").strip()
    patient = str(args.get("patient") or "").strip()
    if not conversation:
        frappe.throw(_("Conversation context is required."))

    convo = safe_ai_get_doc("Chat Conversation", conversation)
    linked_patient = (
        getattr(convo, "linked_patient", None)
        or (
            getattr(convo, "linked_reference_name", None)
            if getattr(convo, "linked_reference_doctype", None) == "Patient"
            else None
        )
    )
    if linked_patient:
        if patient and patient != linked_patient:
            frappe.throw(_("Patient does not match this conversation."))
        return linked_patient
    if patient:
        frappe.throw(_("Patient does not match this conversation."))

    contact = getattr(convo, "contact", None)
    phone_number = ""
    if contact and frappe.db.exists("Chat Contact", contact):
        phone_number = frappe.db.get_value("Chat Contact", contact, "phone_number") or contact

    lead_name = getattr(convo, "linked_crm_lead", None)
    first_name = ""
    department = getattr(convo, "department", None)
    disease = None
    lead_doc = None
    if lead_name and frappe.db.exists("CRM Lead", lead_name):
        lead_doc = safe_ai_get_doc("CRM Lead", lead_name)
        first_name = lead_doc.get("first_name") or lead_doc.get("lead_name") or lead_doc.get("name") or ""
        phone_number = phone_number or lead_doc.get("mobile_no") or lead_doc.get("phone") or ""
        department = department or lead_doc.get("sr_medical_department") or lead_doc.get("department")
        disease = lead_doc.get("sr_lead_disease")
    elif lead_name and frappe.db.exists("Lead", lead_name):
        lead_doc = safe_ai_get_doc("Lead", lead_name)
        first_name = lead_doc.get("first_name") or lead_doc.get("lead_name") or lead_doc.get("name") or ""
        phone_number = phone_number or lead_doc.get("mobile_no") or lead_doc.get("phone") or ""

    patient_context = {
        "conversation": conversation,
        "conversation_doc": convo,
        "contact": contact,
        "phone_number": phone_number,
        "mobile": _last10(phone_number) or str(phone_number or "").strip(),
        "first_name": str(first_name or "").strip(),
        "department": department,
        "disease": disease,
        "lead": lead_name,
        "lead_doc": lead_doc,
        "company": _resolve_company(config.get("company"), args),
        "created_by_agent": tool_label(config),
    }

    mobile_last10 = _last10(str(patient_context.get("mobile") or phone_number))
    if mobile_last10:
        existing_patient = frappe.db.get_value(
            "Patient",
            {"mobile": ["like", f"%{mobile_last10}%"]},
            "name",
        )
        if existing_patient:
            return existing_patient

    if not config.get("create_if_missing"):
        frappe.throw(_("No patient is linked to this conversation."))

    mobile = str(patient_context.get("mobile") or "").strip()
    if not mobile:
        frappe.throw(_("Mobile number is required to create a draft encounter patient."))

    company = patient_context.get("company")
    if not company:
        frappe.throw(_("Company is required to create a patient from chat."))
    _apply_global_defaults(config.get("global_defaults"), args, patient_context)

    assert_ai_doctype_permission("Patient", "write")
    patient_doc = frappe.new_doc("Patient")
    patient_field_values = config.get("patient_field_values")
    if isinstance(patient_field_values, dict):
        for fieldname, spec in patient_field_values.items():
            _set_if(patient_doc, fieldname, _resolve_value(spec, args, patient_context))
    else:
        _set_if(patient_doc, "first_name", str(patient_context.get("first_name") or "").strip() or f"WhatsApp {mobile}")
        _set_if(patient_doc, "patient_name", str(patient_context.get("first_name") or "").strip() or f"WhatsApp {mobile}")
        _set_if(patient_doc, "mobile", mobile)
        _set_if(patient_doc, "status", config.get("status") or "Active")
        _set_if(patient_doc, "sex", config.get("default_sex") or "Male")
        _set_if(patient_doc, "sr_medical_department", _resolve_value(config.get("medical_department"), args, patient_context))
        _set_if(patient_doc, "sr_dpt_disease", disease)
        _set_if(patient_doc, "created_by_agent", tool_label(config))
    patient_doc.insert(ignore_permissions=True, ignore_links=True)

    if config.get("link_to_conversation"):
        try:
            frappe.db.set_value("Chat Conversation", convo.name, "linked_patient", patient_doc.name)
            frappe.db.set_value("Chat Conversation", convo.name, "party_type", "Patient")
            if contact and frappe.db.exists("Chat Contact", contact):
                frappe.db.set_value("Chat Contact", contact, "linked_patient", patient_doc.name)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "WA Configured MCP Patient Link Failed")

    return patient_doc.name


def _apply_global_defaults(defaults: dict[str, Any] | None, args: dict[str, Any], context: dict[str, Any]) -> None:
    if not isinstance(defaults, dict):
        return
    for key, spec in defaults.items():
        fieldname = str(key or "").strip()
        if not fieldname:
            continue
        value = _resolve_value(spec, args, context)
        if value in (None, ""):
            continue
        frappe.defaults.set_global_default(fieldname, value)


def _resolve_value(spec, args: dict[str, Any], context: dict[str, Any]):
    if not isinstance(spec, dict):
        return spec

    if "source" in spec:
        return _apply_value_rules(_source_value(spec, args, context), spec, args, context)

    if "arg" in spec:
        value = args.get(spec.get("arg"))
        if value in (None, "") and "default" in spec:
            value = _special_default(spec.get("default"))
        allowed = spec.get("allowed")
        if allowed and value not in allowed:
            value = _special_default(spec.get("default"))
        return value

    if "fallback_args" in spec:
        for argname in spec.get("fallback_args") or []:
            value = args.get(argname)
            if value not in (None, ""):
                return value
        return _special_default(spec.get("default"))

    if "context" in spec:
        return context.get(spec.get("context"))

    if "patient_field" in spec:
        patient_doc = context.get("patient_doc")
        return patient_doc.get(spec.get("patient_field")) if patient_doc else None

    if "date_arg" in spec:
        value = args.get(spec.get("date_arg"))
        if not value:
            return nowdate()
        try:
            return getdate(value)
        except Exception:
            return nowdate()

    if "link_or_none" in spec:
        return _existing_link_or_none(spec.get("link_or_none"), spec.get("value"))

    if "link_or_default" in spec:
        value = args.get(spec.get("arg")) or spec.get("value")
        return _existing_link_or_default(spec.get("link_or_default"), value)

    if "template" in spec:
        values = _template_values(args, context)
        return str(spec.get("template") or "").format_map(_SafeFormatDict(values)).strip()

    return spec.get("value")


def _source_value(spec: dict[str, Any], args: dict[str, Any], context: dict[str, Any]):
    source = str(spec.get("source") or "").strip()
    if source == "arg":
        return args.get(spec.get("path") or spec.get("arg"))
    if source == "context":
        return _get_path(context, spec.get("path") or spec.get("key"))
    if source == "raw_payload":
        return _raw_payload_value(context.get("conversation") or args.get("conversation"), spec.get("paths") or spec.get("path"))
    if source == "document":
        return _get_path(context.get(spec.get("document")) or context.get("lead_doc"), spec.get("path"))
    if source == "value":
        return spec.get("value")
    return None


def _apply_value_rules(value, spec: dict[str, Any], args: dict[str, Any], context: dict[str, Any]):
    if value in (None, ""):
        if "fallback_template" in spec:
            value = str(spec.get("fallback_template") or "").format_map(
                _SafeFormatDict(_template_values(args, context))
            )
        elif "fallback" in spec:
            value = _special_default(spec.get("fallback"))
        elif "default" in spec:
            value = _special_default(spec.get("default"))

    transform = str(spec.get("transform") or "").strip()
    if transform == "last10":
        value = _last10(value)

    allowed = spec.get("allowed")
    if allowed and value not in allowed:
        value = _special_default(spec.get("fallback") or spec.get("default"))

    link_doctype = spec.get("doctype") or spec.get("link_doctype")
    if link_doctype:
        linked = _existing_link_or_none(link_doctype, value)
        if linked:
            return linked
        fallback = spec.get("fallback")
        linked = _existing_link_or_none(link_doctype, fallback)
        if linked:
            return linked
        if spec.get("use_default_link"):
            return _existing_link_or_default(link_doctype, None)
        return None

    return value


def _raw_payload_value(conversation: str | None, paths) -> Any:
    if isinstance(paths, str):
        paths = [paths]
    paths = [str(path or "").strip() for path in (paths or []) if str(path or "").strip()]
    if not conversation or not paths:
        return None
    rows = frappe.get_all(
        "Chat Message",
        filters={"conversation": conversation},
        fields=["raw_payload"],
        order_by="creation desc, name desc",
        limit_page_length=10,
    )
    for row in rows:
        try:
            payload = frappe.parse_json(row.get("raw_payload") or "{}")
        except Exception:
            payload = {}
        for path in paths:
            value = _get_path(payload, path)
            if value not in (None, ""):
                return value
    return None


def _get_path(value, path: str | None):
    if not path:
        return value
    current = value
    for part in str(path).split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except Exception:
                return None
        elif hasattr(current, "get"):
            current = current.get(part)
        else:
            return getattr(current, part, None)
    return current


def _template_values(args: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    values = {key: "" if value is None else value for key, value in args.items()}
    values.update({key: "" if value is None else value for key, value in context.items() if key != "patient_doc"})
    values.setdefault("today", nowdate())
    values.setdefault("nowtime", nowtime())
    return values


class _SafeFormatDict(dict):
    def __missing__(self, key):
        return ""


def _resolve_company(spec, args: dict[str, Any]) -> str | None:
    if isinstance(spec, dict):
        value = args.get(spec.get("arg")) or spec.get("value")
        return _existing_link_or_default(spec.get("link_or_default") or "Company", value)
    return _existing_link_or_default("Company", str(spec or args.get("company") or "").strip())


def _special_default(value):
    if value == "today":
        return nowdate()
    if value == "nowtime":
        return nowtime()
    return value


def _load_config(value: str | None) -> dict[str, Any]:
    if not value:
        frappe.throw(_("Execution Config is required for this MCP tool."))
    try:
        data = json.loads(value)
    except Exception:
        frappe.throw(_("Execution Config must be valid JSON."))
    if not isinstance(data, dict):
        frappe.throw(_("Execution Config must be a JSON object."))
    return data


def _existing_link_or_none(doctype: str | None, value: str | None) -> str | None:
    doctype = str(doctype or "").strip()
    value = str(value or "").strip()
    if not doctype or not value or not frappe.db.exists("DocType", doctype):
        return None
    return value if frappe.db.exists(doctype, value) else None


def _existing_link_or_default(doctype: str | None, value: str | None) -> str | None:
    existing = _existing_link_or_none(doctype, value)
    if existing:
        return existing
    doctype = str(doctype or "").strip()
    if not doctype or not frappe.db.exists("DocType", doctype):
        return None
    rows = frappe.get_all(doctype, pluck="name", limit_page_length=1)
    return rows[0] if rows else None


def _last10(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


def _set_if(doc, fieldname: str, value) -> None:
    if value is not None and doc.meta.has_field(fieldname):
        doc.set(fieldname, value)


def tool_label(config: dict[str, Any]) -> str:
    return str(config.get("created_by_agent") or "WA Configured MCP")
