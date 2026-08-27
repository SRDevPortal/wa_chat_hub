from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


ALLOWED_SOURCES = {"value", "origin_message", "latest_message", "collected"}
RESERVED_ARGUMENTS = {"conversation", "message", "channel_account", "patient", "confirmation_token"}
ALLOWED_CONFIRMATION_MODES = {"challenge", "interakt_template"}
ALLOWED_VALIDATORS = {
    "min_length",
    "max_length",
    "min_words",
    "excludes_digit",
    "contains_alpha",
    "pattern",
    "contains_digit",
    "contains_any",
    "contains_all",
    "digit_group_lengths_any",
}
ALLOWED_PREFILL_SOURCES = {
    "linked_record_fields",
    "phone_match_fields",
    "recent_message_regex",
}


class WAAIWorkflow(Document):
    def validate(self):
        version = cint(self.workflow_version)
        expiry = cint(self.expiry_minutes)
        if version < 1:
            frappe.throw(_("Workflow Version must be at least 1."))
        if expiry < 1 or expiry > 43200:
            frappe.throw(_("Expiry Minutes must be between 1 and 43200."))

        definition = _parse_definition(self.definition)
        collect = definition.get("collect") or []
        if not isinstance(collect, list) or len(collect) > 20:
            frappe.throw(_("Workflow collect must be a list with at most 20 fields."))
        names = set()
        for step in collect:
            if not isinstance(step, dict):
                frappe.throw(_("Each workflow collect step must be an object."))
            fieldname = str(step.get("field") or "").strip()
            if not fieldname or fieldname in names:
                frappe.throw(_("Workflow collect field names must be present and unique."))
            names.add(fieldname)
            if not str(step.get("prompt") or "").strip():
                frappe.throw(_("Each workflow collect step requires a prompt."))
            validation = step.get("validation") or {}
            if not isinstance(validation, dict):
                frappe.throw(_("Workflow validation must be an object."))
            unsupported = set(validation) - ALLOWED_VALIDATORS
            if unsupported:
                frappe.throw(_("Unsupported workflow validators: {0}").format(", ".join(sorted(unsupported))))
            configured_pattern = str(validation.get("pattern") or "").strip()
            if configured_pattern:
                try:
                    re.compile(configured_pattern)
                except re.error:
                    frappe.throw(_("Workflow validation pattern must be a valid regular expression."))

            prefill = step.get("prefill")
            if prefill is not None:
                if not isinstance(prefill, dict):
                    frappe.throw(_("Workflow prefill must be an object."))
                sources = prefill.get("sources")
                if sources is None:
                    sources = [prefill]
                if not isinstance(sources, list) or not sources or len(sources) > 10:
                    frappe.throw(_("Workflow prefill sources must contain between 1 and 10 items."))
                for source_config in sources:
                    _validate_prefill_source(source_config)

        confirmation = definition.get("confirmation") or {}
        if not isinstance(confirmation, dict):
            frappe.throw(_("Workflow confirmation must be an object."))
        mode = str(confirmation.get("mode") or "").strip()
        if mode not in ALLOWED_CONFIRMATION_MODES:
            frappe.throw(_("Workflow confirmation mode must be challenge or interakt_template."))
        if not str(confirmation.get("prompt") or "").strip():
            frappe.throw(_("Workflow confirmation requires a prompt."))

        arguments = definition.get("arguments") or {}
        if not isinstance(arguments, dict):
            frappe.throw(_("Workflow arguments must be an object."))
        reserved = set(arguments) & RESERVED_ARGUMENTS
        if reserved:
            frappe.throw(_("Workflow arguments cannot set reserved context fields: {0}").format(", ".join(sorted(reserved))))
        for fieldname, spec in arguments.items():
            if not isinstance(spec, dict):
                frappe.throw(_("Workflow argument mappings must be objects."))
            source = str(spec.get("source") or "").strip()
            if source not in ALLOWED_SOURCES:
                frappe.throw(_("Unsupported source for workflow argument {0}.").format(fieldname))
            if source == "collected" and str(spec.get("field") or "").strip() not in names:
                frappe.throw(_("Workflow argument {0} references an unknown collected field.").format(fieldname))

        if frappe.db.exists("WA MCP Tool Endpoint", self.action_tool):
            access_mode = frappe.db.get_value("WA MCP Tool Endpoint", self.action_tool, "access_mode")
            if str(access_mode or "").lower() == "write" and not confirmation:
                frappe.throw(_("Write workflows require explicit confirmation configuration."))


def _parse_definition(raw):
    try:
        value = frappe.parse_json(raw)
    except Exception:
        frappe.throw(_("Workflow Definition must be valid JSON."))
    if not isinstance(value, dict):
        frappe.throw(_("Workflow Definition must be a JSON object."))
    return value


def _validate_prefill_source(source_config) -> None:
    if not isinstance(source_config, dict):
        frappe.throw(_("Each workflow prefill source must be an object."))
    source = str(source_config.get("source") or "").strip()
    if source not in ALLOWED_PREFILL_SOURCES:
        frappe.throw(_("Unsupported workflow prefill source: {0}").format(source))

    if source == "recent_message_regex":
        patterns = source_config.get("patterns") or []
        if not isinstance(patterns, list) or not patterns or len(patterns) > 20:
            frappe.throw(_("Workflow prefill patterns must contain between 1 and 20 items."))
        for pattern in patterns:
            try:
                re.compile(str(pattern))
            except re.error:
                frappe.throw(_("Workflow prefill patterns must be valid regular expressions."))
        max_messages = cint(source_config.get("max_messages") or 30)
        if max_messages < 1 or max_messages > 100:
            frappe.throw(_("Workflow prefill max_messages must be between 1 and 100."))
        return

    doctype = str(source_config.get("doctype") or "").strip()
    value_fields = source_config.get("value_fields") or []
    if not doctype or not _valid_field_list(value_fields):
        frappe.throw(_("Record prefill sources require a DocType and value_fields."))
    if source == "linked_record_fields" and not str(
        source_config.get("conversation_link_field") or ""
    ).strip():
        frappe.throw(_("Linked-record prefill requires conversation_link_field."))
    if source == "phone_match_fields":
        if not _valid_field_list(source_config.get("phone_fields") or []):
            frappe.throw(_("Phone-match prefill requires phone_fields."))
        max_records = cint(source_config.get("max_records") or 5)
        if max_records < 1 or max_records > 20:
            frappe.throw(_("Phone-match prefill max_records must be between 1 and 20."))


def _valid_field_list(value) -> bool:
    return (
        isinstance(value, list)
        and 0 < len(value) <= 20
        and all(str(fieldname or "").strip() for fieldname in value)
    )
