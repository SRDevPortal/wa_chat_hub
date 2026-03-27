import frappe


def get_context(context):
    context.no_cache = 1
    context.title = "WA Chat Hub"
    context.erp_actions = {
        "lead": "Lead",
        "encounter": "Patient Encounter",
        "support_ticket": "Issue",
        "patient": "Patient",
    }
    return context
