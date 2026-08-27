from __future__ import annotations

import frappe


class PatientTemplateDeliveryError(frappe.ValidationError):
    """Base error carrying whether a definitely-unsent delivery may be retried."""

    retryable = False

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class PatientTemplateNotSentError(PatientTemplateDeliveryError):
    """The provider definitely did not accept the message."""


class PatientTemplateOutcomeUnknownError(PatientTemplateDeliveryError):
    """The provider may have accepted the message; automatic retry is unsafe."""

