frappe.ui.form.on("Patient", {
    refresh(frm) {
        if (frm.is_new()) {
            return;
        }

        frm.add_custom_button(__("WA Chat Hub"), function() {
            frappe.call({
                method: "wa_chat_hub.api.chat.get_existing_conversation_for_patient",
                args: {
                    patient: frm.doc.name,
                },
                freeze: true,
                freeze_message: __("Finding existing WhatsApp chat..."),
                callback(r) {
                    const data = r.message || {};
                    if (data.success && data.conversation) {
                        set_patient_scope_and_open(data.conversation);
                        return;
                    }

                    frappe.msgprint({
                        title: __("WhatsApp Chat"),
                        message: data.message || __("No existing WhatsApp chat found for this Patient number."),
                        indicator: "orange",
                    });
                },
            });
        });
    },
});

function set_patient_scope_and_open(conversation) {
    frappe.call({
        method: "wa_chat_hub.api.chat.set_chat_hub_scope",
        type: "POST",
        args: {
            reference_doctype: "Patient",
            locked: 1,
        },
        callback() {
            window.location.href = `/app/wa-chat-hub?reference_doctype=Patient&lock_reference_filter=1&conversation=${encodeURIComponent(conversation)}`;
        },
    });
}
