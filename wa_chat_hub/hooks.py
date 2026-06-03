app_name = "wa_chat_hub"
app_title = "WA Chat Hub"
app_publisher = "SAI"
app_description = "Unified WhatsApp operations hub for ERPNext"
app_email = "admin@example.com"
app_license = "MIT"

app_include_css = ["/assets/wa_chat_hub/css/wa_chat_hub.css"]
app_include_js = ["/assets/wa_chat_hub/js/remote_attachment_links.js"]

after_install = "wa_chat_hub.setup_workspace.run"
after_migrate = "wa_chat_hub.migrate.after_migrate"

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
}
