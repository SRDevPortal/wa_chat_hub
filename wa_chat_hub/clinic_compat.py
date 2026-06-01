from __future__ import annotations

import frappe


CRM_LEAD_PQC_PATHS = (
    "siya_clinic.api.crm_lead.access.crm_lead_pqc",
    "sriaas_clinic.api.crm_lead.access.crm_lead_pqc",
)


def get_crm_lead_pqc():
    """Return the installed clinic app's CRM Lead permission query hook."""
    for path in CRM_LEAD_PQC_PATHS:
        try:
            return frappe.get_attr(path)
        except Exception:
            continue
    return None
