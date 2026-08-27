from __future__ import annotations

import ast

import frappe
from frappe import _
from frappe.model.document import Document


class WAAppUpdateScript(Document):
    def before_insert(self):
        self.enabled = 0
        self.status = "Draft"

    def validate(self):
        self.update_name = (self.update_name or "").strip()
        self.update_version = (self.update_version or "").strip()
        self._validate_python("Apply Script", self.apply_script)
        self._validate_python("Revert Script", self.revert_script)

        if not self.is_new():
            previous = self.get_doc_before_save()
            protected_fields = (
                "apply_script",
                "revert_script",
                "run_migration",
                "clear_cache_after_execution",
                "restart_required",
            )
            if previous and previous.enabled and any(
                self.get(fieldname) != previous.get(fieldname) for fieldname in protected_fields
            ):
                frappe.throw(_("Disable this update before changing its scripts or execution options."))
            if previous and (
                int(self.enabled or 0) != int(previous.enabled or 0)
                or self.status != previous.status
            ):
                frappe.throw(_("Use the Enable, Disable, or Run Again actions to change update state."))

    def after_insert(self):
        from wa_chat_hub.app_update import write_update_log

        write_update_log(self, action="Created", success=True, result="Update definition created.")

    def on_trash(self):
        if self.enabled:
            frappe.throw(_("Disable this update before deleting it."))

    @staticmethod
    def _validate_python(label: str, script: str | None) -> None:
        try:
            ast.parse(script or "")
        except SyntaxError as exc:
            frappe.throw(_("{0} has invalid Python syntax at line {1}: {2}").format(label, exc.lineno, exc.msg))
