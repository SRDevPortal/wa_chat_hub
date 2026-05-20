app_name = "wa_chat_hub"
app_title = "WA Chat Hub"
app_publisher = "SAI"
app_description = "Unified WhatsApp operations hub for ERPNext"
app_email = "admin@example.com"
app_license = "MIT"

app_include_css = ["/assets/wa_chat_hub/css/wa_chat_hub.css"]
app_include_js = ["/assets/wa_chat_hub/js/wa_navbar.js"]

doctype_list_js = {
    "CRM Lead": "public/js/reference_open_chat_list.js",
    "Patient": "public/js/reference_open_chat_list.js",
    "Patient Encounter": "public/js/reference_open_chat_list.js",
}

after_install = "wa_chat_hub.setup_workspace.run"
after_migrate = "wa_chat_hub.migrate.after_migrate"

fixtures = [
    {"dt": "DocType", "filters": [["module", "=", "WA Chat Hub"]]},
    {"dt": "Page", "filters": [["module", "=", "WA Chat Hub"]]},
    {"dt": "Workspace", "filters": [["module", "=", "WA Chat Hub"]]},
]

doc_events = {
    "Chat Message": {
        "after_insert": "wa_chat_hub.lead_ai.on_chat_message_after_insert",
    },
}
