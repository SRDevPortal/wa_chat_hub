from __future__ import annotations

import json

import frappe
from frappe.model.document import Document

from wa_chat_hub.policy import (
    POLICY_MAX_SERIALIZED_BYTES,
    POLICY_SECTION_FIELDS,
    invalidate_policy_cache,
    parse_policy_object,
)


class WAAIPolicyBundle(Document):
    def validate(self) -> None:
        serialized_size = 0
        for fieldname in POLICY_SECTION_FIELDS:
            value = parse_policy_object(self.get(fieldname), fieldname=fieldname)
            encoded = json.dumps(value, ensure_ascii=False, indent=2)
            serialized_size += len(encoded.encode("utf-8"))
            self.set(fieldname, encoded)
        if serialized_size > POLICY_MAX_SERIALIZED_BYTES:
            frappe.throw(
                f"Combined policy JSON cannot exceed {POLICY_MAX_SERIALIZED_BYTES} bytes."
            )
        self.policy_version = max(1, int(self.policy_version or 1))

    def on_update(self) -> None:
        invalidate_policy_cache(self.name)

    def on_trash(self) -> None:
        invalidate_policy_cache(self.name)
