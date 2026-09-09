app_name = "wa_chat_hub"
app_title = "WA Chat Hub"
app_publisher = "SAI"
app_description = "Unified WhatsApp operations hub for ERPNext"
app_email = "admin@example.com"
app_license = "MIT"
required_apps = ["erpnext"]

app_include_css = ["/assets/wa_chat_hub/css/wa_chat_hub.css"]
app_include_js = ["/assets/wa_chat_hub/js/remote_attachment_links.js", "/assets/wa_chat_hub/js/reference_chat.js"]

after_install = "wa_chat_hub.setup_workspace.run"
after_migrate = "wa_chat_hub.migrate.after_migrate"
before_tests = "wa_chat_hub.tests.utils.before_tests"

lead_custom_fieldnames = [
    "lead_score",
    "lead_lan",
    "lead_temperature",
    "shipkia_section",
    "shipkia_business_type",
    "shipkia_business_name",
    "shipkia_monthly_shipments",
    "shipkia_current_aggregator_status",
    "shipkia_current_aggregator_name",
    "shipkia_current_aggregator_raw",
    "shipkia_current_aggregator_verified",
    "shipkia_current_shipping_rate",
    "shipkia_rto_percentage",
    "shipkia_route_column_break",
    "shipkia_pickup_city",
    "shipkia_delivery_city",
    "shipkia_average_weight",
    "shipkia_rate_shared",
    "shipkia_ai_qualification_score",
    "shipkia_ai_messages_count",
    "shipkia_ai_details_collected",
    "shipkia_ai_last_message_at",
    "shipkia_context_updated_at",
    "shipkia_context_completed",
    "shipkia_ai_context_complete",
    "shipkia_requirement_details",
    "shipkia_status_column_break",
    "shipkia_lead_temperature",
    "shipkia_ai_lead_temperature",
    "shipkia_qualification_status",
    "shipkia_ai_qualification_status",
    "shipkia_ai_onboarding_stage",
    "shipkia_ai_onboarding_assisted",
    "shipkia_sales_stage",
    "shipkia_lead_source",
    "shipkia_first_contact_channel",
    "shipkia_service_scope_status",
    "shipkia_disqualification_reason",
    "shipkia_max_shipment_weight_kg",
]

fixtures = [
    {"dt": "DocType", "filters": [["module", "=", "WA Chat Hub"]]},
    {"dt": "Page", "filters": [["module", "=", "WA Chat Hub"]]},
    {"dt": "Workspace", "filters": [["module", "=", "WA Chat Hub"]]},
    {"dt": "Custom Field", "filters": [["dt", "=", "Lead"], ["fieldname", "in", lead_custom_fieldnames]]},
]

doctype_list_js = {
    "Lead": "public/js/reference_open_chat_list.js",
    "Customer": "public/js/reference_open_chat_list.js",
}

doctype_js = {doctype: "public/js/reference_chat_form.js" for doctype in ("Lead", "Customer")}

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
    "Lead": {
        "validate": "wa_chat_hub.maintenance.phone_backfill.sync_phone_keys",
    },
    "Customer": {
        "validate": "wa_chat_hub.maintenance.phone_backfill.sync_phone_keys",
    },
}
