frappe.ui.form.on("WA Chat Hub Settings", {
    refresh(frm) {
        frm.trigger("toggle_autopilot_reply_batching_fields");
        frm.trigger("refresh_app_update_summary");
    },

    enable_autopilot_reply_batching(frm) {
        if (!frm.doc.enable_autopilot_reply_batching) {
            frm.set_value("text_autopilot_reply_delay_seconds", 0);
            frm.set_value("media_autopilot_reply_delay_seconds", 0);
        }
        frm.trigger("toggle_autopilot_reply_batching_fields");
    },

    toggle_autopilot_reply_batching_fields(frm) {
        const show_delay_fields = !!frm.doc.enable_autopilot_reply_batching;
        frm.toggle_display("text_autopilot_reply_delay_seconds", show_delay_fields);
        frm.toggle_display("media_autopilot_reply_delay_seconds", show_delay_fields);
    },

    new_app_update() {
        frappe.new_doc("WA App Update Script");
    },

    manage_app_updates() {
        frappe.set_route("List", "WA App Update Script");
    },

    view_app_update_logs() {
        frappe.set_route("List", "WA App Update Log");
    },

    refresh_app_update_summary(frm) {
        if (!frm.doc.enable_app_update_system) {
            frm.doc.active_app_update_count = 0;
            frm.doc.last_app_update_result = __("App update system is disabled.");
            frm.refresh_fields(["active_app_update_count", "last_app_update_result"]);
            return;
        }

        frappe.call({
            method: "wa_chat_hub.app_update.get_update_summary",
            callback(r) {
                const summary = r.message || {};
                const latest = summary.latest;
                frm.doc.active_app_update_count = summary.active_count || 0;
                frm.doc.last_app_update_result = latest
                    ? `${latest.update_script}: ${latest.action} (${latest.success ? __("Success") : __("Failed")})`
                    : __("No app updates have been executed yet.");
                frm.refresh_fields(["active_app_update_count", "last_app_update_result"]);
            },
        });
    },
});
