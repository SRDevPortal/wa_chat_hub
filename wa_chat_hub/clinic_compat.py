from __future__ import annotations

import frappe


CRM_LEAD_PQC_PATHS = (
    "siya_clinic.api.crm_lead.access.crm_lead_pqc",
    "sriaas_clinic.api.crm_lead.access.crm_lead_pqc",
)


def get_crm_lead_pqc():
    """Return the installed clinic app's Lead permission query hook."""
    installed_apps = set(frappe.get_installed_apps())

    for path in CRM_LEAD_PQC_PATHS:
        app_name = path.split(".", 1)[0]
        if app_name not in installed_apps:
            continue

        try:
            return frappe.get_attr(path)
        except Exception:
            continue
    return None
