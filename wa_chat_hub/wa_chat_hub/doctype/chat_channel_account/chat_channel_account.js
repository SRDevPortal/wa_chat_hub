frappe.ui.form.on("Chat Channel Account", {
    refresh(frm) {
        // Toggle the WABA settings visibility just in case depends_on doesn't fire immediately
        frm.trigger("channel_type");

        if (frm.doc.channel_type === "Interakt" && !frm.is_new()) {
            const base = `${window.location.origin}/api/method/wa_chat_hub.api.webhook.receive_interakt`;
            const webhookUrl = `${base}?channel_account=${encodeURIComponent(frm.doc.name)}`;
            frm.set_intro(
                `<div><strong>${__('Interakt Webhook URL')}</strong><br>`
                + `<code style="word-break:break-all;">${frappe.utils.escape_html(webhookUrl)}</code><br>`
                + `<span class="text-muted">${__('Use this URL in Interakt Developer Settings for this account. Each account should have its own webhook secret.')}</span></div>`,
                "blue"
            );
        } else {
            frm.set_intro("");
        }
        
        // Add QR Button for Personal WA if not active
        if (frm.doc.channel_type === "Personal WhatsApp" && frm.doc.connector_status !== "Active") {
            frm.add_custom_button(__("Link Device via QR"), function() {
                // Show QR Modal Placeholder
                let d = new frappe.ui.Dialog({
                    title: 'Scan WhatsApp QR',
                    fields: [
                        {
                            fieldname: 'qr_placeholder',
                            fieldtype: 'HTML',
                            options: `
                                <div style="text-align: center; padding: 20px;">
                                    <p class="text-muted">Fetching QR Session Token...</p>
                                    <div class="skeleton-state" style="width: 250px; height: 250px; background: #f3f3f3; margin: auto; display: flex; align-items: center; justify-content: center; border-radius: 8px;">
                                        <i class="fa fa-qrcode" style="font-size: 50px; color: #ccc;"></i>
                                    </div>
                                    <p style="margin-top: 15px; font-size: 12px; color: gray;">
                                        <strong>Integration Note:</strong> This modal will be wired up to your bailey's/puppeteer backend API when the research phase completes.
                                    </p>
                                </div>
                            `
                        }
                    ],
                    size: 'small',
                    primary_action_label: 'Mock Connect',
                    primary_action(values) {
                        frappe.show_alert({message: __('Device magically linked! (Mock)'), indicator: 'green'});
                        frm.set_value("connector_status", "Active");
                        frm.save();
                        d.hide();
                    }
                });
                
                d.show();
            }).addClass("btn-primary");
        }
    },
    
    channel_type(frm) {
        // Enforce visibility dynamically
        let is_official = frm.doc.channel_type === "Official WhatsApp";
        let fields_to_toggle = ["provider_base_url", "x_api_key", "x_tenant_id", "x_user_id", "waba_id", "phone_id", "sb_api_config"];
        
        fields_to_toggle.forEach(f => {
            frm.toggle_display(f, is_official);
        });
    }
});
