frappe.ui.form.on("Chat Channel Account", {
    refresh(frm) {
        // Toggle the WABA settings visibility just in case depends_on doesn't fire immediately
        frm.trigger("channel_type");

        if (frm.doc.channel_type === "Interakt" && !frm.is_new()) {
            if (frm.perm.some(p => p.write)) {
                frm.add_custom_button(__("Fetch / Refresh Approved Templates"), () => frm.trigger("fetch_interakt_templates"));
                frm.add_custom_button(__("Select Chat Templates"), () => select_interakt_chat_templates(frm));
            }

            frappe.call({
                method: "wa_chat_hub.api.webhook.get_interakt_webhook_url",
                args: { channel_account: frm.doc.name },
                callback(r) {
                    const webhookUrl = (r.message && r.message.url) || "";
                    frm.set_intro(
                        `<div><strong>${__("Interakt Webhook URL")}</strong><br>`
                        + `<code style="word-break:break-all;">${frappe.utils.escape_html(webhookUrl)}</code><br>`
                        + `<span class="text-muted">${__(
                            "Paste this URL in Interakt Developer Settings. Use the same Interakt Webhook Secret as on this form. "
                            + "Set site host_name (ngrok/public URL) in site_config — not localhost."
                        )}</span></div>`,
                        "blue"
                    );
                },
            });
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
    
    async fetch_interakt_templates(frm) {
        if (frm.is_new() || frm.is_dirty()) {
            frappe.msgprint(__("Save this account and its API key before fetching templates."));
            return;
        }
        const result = await frappe.call({
            method: "wa_chat_hub.interakt.template_selection.refresh_templates",
            args: {channel_account: frm.doc.name},
            freeze: true,
            freeze_message: __("Fetching approved templates using this account's saved Interakt API key?"),
        });
        await frm.reload_doc();
        frappe.show_alert({message: __("Fetched {0} approved templates. Select templates and save.", [result.message.count]), indicator: "green"});
    },

    select_interakt_templates(frm) {
        select_interakt_chat_templates(frm);
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

function select_interakt_chat_templates(frm) {
    const rows = (frm.doc.interakt_template_catalog || []).filter(row => row.approval_status === "Approved");
    const escape = frappe.utils.escape_html;
    const selected = new Set(rows.filter(row => row.enabled_in_chat).map(row => row.name));
    const dialog = new frappe.ui.Dialog({
        title: __("Select Templates for WA Chat Hub and Vobiz"),
        size: "large",
        fields: [
            {fieldname: "search", fieldtype: "Data", label: __("Search templates"), onchange: () => render()},
            {fieldname: "templates", fieldtype: "HTML"},
        ],
        primary_action_label: __("Apply Selection"),
        primary_action() {
            rows.forEach(row => {row.enabled_in_chat = selected.has(row.name) ? 1 : 0;});
            frm.dirty();
            frm.refresh_field("interakt_template_catalog");
            dialog.hide();
            frappe.show_alert(__("Save the account to apply this selection to both chats."));
        },
    });
    const wrapper = dialog.fields_dict.templates.$wrapper;
    function render() {
        const query = (dialog.get_value("search") || "").toLowerCase();
        const visible = rows.filter(row => [row.template_name, row.display_name, row.language_code, row.category].join(" ").toLowerCase().includes(query));
        wrapper.html(`<div class="mb-3"><button class="btn btn-default btn-xs" data-action="all">${__("Select All")}</button>
            <button class="btn btn-default btn-xs" data-action="none">${__("Clear Selection")}</button>
            <span class="text-muted ml-2">${selected.size} / ${rows.length} ${__("selected")}</span></div>
            <div style="max-height:400px;overflow:auto">${visible.map(row => `<label class="d-block p-2 border-bottom">
                <input type="checkbox" data-row="${escape(row.name)}" ${selected.has(row.name) ? "checked" : ""}>
                <strong>${escape(row.display_name || row.template_name)}</strong> (${escape(row.language_code || "en")})
                <div class="text-muted">${escape(row.template_name)} ? ${escape(row.category || "")}</div>
                <div>${escape(row.body_preview || "")}</div></label>`).join("") || __("No approved templates found. Fetch templates first.")}</div>`);
        wrapper.find("[data-row]").on("change", function() {
            this.checked ? selected.add(this.dataset.row) : selected.delete(this.dataset.row);
            render();
        });
        wrapper.find('[data-action="all"]').on("click", () => {rows.forEach(row => selected.add(row.name)); render();});
        wrapper.find('[data-action="none"]').on("click", () => {selected.clear(); render();});
    }
    dialog.show();
    dialog.fields_dict.search.$input.on("input", render);
    render();
}
