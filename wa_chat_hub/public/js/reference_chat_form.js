for (const doctype of ["Lead", "Customer", "CRM Lead"]) {
    frappe.ui.form.on(doctype, {
        refresh(frm) {
            if (frm.is_new()) return;
            frm.add_custom_button(__("Open WhatsApp Chat"), () => {
                wa_chat_hub.open_reference_chat(frm.doctype, frm.doc.name);
            });
        },
    });
}
