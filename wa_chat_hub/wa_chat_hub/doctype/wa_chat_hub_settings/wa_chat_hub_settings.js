frappe.ui.form.on("WA Chat Hub Settings", {
    refresh(frm) {
        frm.trigger("toggle_autopilot_reply_batching_fields");
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
});
