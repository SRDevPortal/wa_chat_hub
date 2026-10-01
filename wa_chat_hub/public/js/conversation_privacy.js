frappe.ui.form.on('Chat Conversation', {
    refresh(frm) {
        if (!frm.doc.__wa_number_privacy) return;
        frm.set_df_property('contact', 'fieldtype', 'Data');
        frm.set_df_property('contact', 'read_only', 1);
        frm.set_df_property('contact', 'label', __('Masked Contact'));
        for (const field of ['ai_workflow_state', 'pending_patient_request', 'pending_request_message']) {
            if (frm.fields_dict[field]) frm.set_df_property(field, 'hidden', 1);
        }
    }
});
