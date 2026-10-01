app_name = "wa_chat_hub"
app_title = "WA Chat Hub"
app_publisher = "SAI"
app_description = "Unified WhatsApp operations hub for ERPNext"
app_email = "admin@example.com"
app_license = "MIT"

app_include_css = ["/assets/wa_chat_hub/css/wa_chat_hub.css"]
app_include_js = ["/assets/wa_chat_hub/js/remote_attachment_links.js"]

after_install = [
    "wa_chat_hub.setup_workspace.run",
    "wa_chat_hub.maintenance.notification_indexes.ensure_notification_event_index",
]
after_migrate = "wa_chat_hub.migrate.after_migrate"
before_tests = "wa_chat_hub.tests.utils.before_tests"

fixtures = [
    {"dt": "DocType", "filters": [["module", "=", "WA Chat Hub"]]},
    {"dt": "Page", "filters": [["module", "=", "WA Chat Hub"]]},
    {"dt": "Workspace", "filters": [["module", "=", "WA Chat Hub"]]},
    {"dt": "Custom Field", "filters": [["dt", "=", "Lead"], ["fieldname", "in", ["lead_score", "lead_lan", "lead_temperature"]]]},
    {"dt": "Custom Field", "filters": [["dt", "=", "CRM Lead"], ["fieldname", "in", ["lead_score", "lead_lan", "lead_temperature"]]]},
]

doctype_list_js = {
    "CRM Lead": "public/js/reference_open_chat_list.js",
    "Patient": "public/js/reference_open_chat_list.js",
    "Patient Encounter": "public/js/reference_open_chat_list.js",
}

doctype_js = {
    "Patient": "public/js/patient_chat_button.js",
}

permission_query_conditions = {
    "Chat Conversation": "wa_chat_hub.permissions.chat_conversation_pqc",
    "Chat Contact": "wa_chat_hub.permissions.chat_contact_pqc",
    "Chat Message": "wa_chat_hub.permissions.chat_message_pqc",
}

has_permission = {
    "Chat Conversation": "wa_chat_hub.permissions.chat_conversation_has_permission",
    "Chat Contact": "wa_chat_hub.permissions.chat_contact_has_permission",
    "Chat Message": "wa_chat_hub.permissions.chat_message_has_permission",
}

doc_events = {
    "Chat Message": {
        "after_insert": [
            "wa_chat_hub.api.ai_bot.on_message_received",
            "wa_chat_hub.lead_ai.on_chat_message_after_insert",
        ],
    },
    "Patient": {
        "validate": "wa_chat_hub.maintenance.phone_backfill.sync_phone_keys",
        "after_insert": "wa_chat_hub.identity.reconcile_patient_conversations",
        "on_update": "wa_chat_hub.identity.reconcile_patient_conversations",
    },
    "CRM Lead": {
        "validate": "wa_chat_hub.maintenance.phone_backfill.sync_phone_keys",
    },
    "Lead": {
        "validate": "wa_chat_hub.maintenance.phone_backfill.sync_phone_keys",
    },
    "Customer": {
        "validate": "wa_chat_hub.maintenance.phone_backfill.sync_phone_keys",
    },
    "Patient Encounter": {
        "after_insert": "wa_chat_hub.identity.reconcile_patient_encounter",
        "on_update": "wa_chat_hub.identity.reconcile_patient_encounter",
    },
}

# Preserve the clinic privacy adapters for other documents.
override_whitelisted_methods = {
    "frappe.desk.reportview.get": "wa_chat_hub.desk_privacy.reportview_get",
    "frappe.desk.reportview.get_list": "wa_chat_hub.desk_privacy.reportview_get_list",
}

override_whitelisted_methods.update({
    "frappe.desk.form.load.getdoc": "wa_chat_hub.desk_privacy.getdoc",
    "frappe.desk.form.save.savedocs": "wa_chat_hub.desk_privacy.savedocs",
})

_existing_conversation_js = doctype_js.get("Chat Conversation", [])
if isinstance(_existing_conversation_js, str):
    _existing_conversation_js = [_existing_conversation_js]
doctype_js["Chat Conversation"] = [*_existing_conversation_js, "public/js/conversation_privacy.js"]

# Narrow generic reads only; retain existing clinic adapters and permissions.
override_whitelisted_methods.update({
    "frappe.client.get": "wa_chat_hub.client_privacy.get",
    "frappe.client.get_list": "wa_chat_hub.client_privacy.get_list",
    "frappe.client.get_value": "wa_chat_hub.client_privacy.get_value",
})

# Native v1/v2 Conversation GET only; framework permissions run before projection.
after_request = ["wa_chat_hub.rest_privacy.protect_conversation_read"]

# Preserve existing export adapters; mask only reviewed chat document downloads.
override_whitelisted_methods.update({
    "frappe.core.doctype.data_export.exporter.export_data": "wa_chat_hub.export_privacy.export_data",
    "frappe.desk.reportview.export_query": "wa_chat_hub.export_privacy.export_query",
})

# Legacy whitelisted export aliases must use the same reviewed boundaries.
for _export_module in ("privacy_shield.outputs", "sriaas_clinic.api.crm_lead.privacy_outputs"):
    override_whitelisted_methods.update({
        _export_module + ".export_data": "wa_chat_hub.export_privacy.export_data",
        _export_module + ".export_query": "wa_chat_hub.export_privacy.export_query",
    })

# Legacy read RPCs use the existing scoped adapters. Non-chat calls delegate
# unchanged; imports inside the adapters are direct calls, not RPC redispatch.
override_whitelisted_methods.update({
    "privacy_shield.desk.getdoc": "wa_chat_hub.desk_privacy.getdoc",
    "privacy_shield.desk.get": "wa_chat_hub.client_privacy.get",
    "privacy_shield.listing.get_list": "wa_chat_hub.client_privacy.get_list",
    "privacy_shield.listing.get_value": "wa_chat_hub.client_privacy.get_value",
    "privacy_shield.listing.reportview_get": "wa_chat_hub.desk_privacy.reportview_get",
    "privacy_shield.listing.reportview_get_list": "wa_chat_hub.desk_privacy.reportview_get_list",
    "sriaas_clinic.api.crm_lead.privacy.desk_getdoc": "wa_chat_hub.desk_privacy.getdoc",
    "sriaas_clinic.api.crm_lead.privacy.client_get": "wa_chat_hub.client_privacy.get",
    "sriaas_clinic.api.crm_lead.privacy.client_get_list": "wa_chat_hub.client_privacy.get_list",
    "sriaas_clinic.api.crm_lead.privacy.client_get_value": "wa_chat_hub.client_privacy.get_value",
    "sriaas_clinic.api.crm_lead.privacy.reportview_get": "wa_chat_hub.desk_privacy.reportview_get",
    "sriaas_clinic.api.crm_lead.privacy.reportview_get_list": "wa_chat_hub.desk_privacy.reportview_get_list",
})
