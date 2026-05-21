frappe.ui.form.on("WA Channel Pipeline Map", {
	refresh(frm) {
		frm.set_intro(
			__(
				"One row per Interakt account: links account + SR Lead Pipeline (CRM Lead) + Medical Department (Patient sr_medical_department)."
			),
			"blue"
		);

		if (frm.doc.chat_channel_account) {
			frappe.db.get_value("Chat Channel Account", frm.doc.chat_channel_account, ["channel_type"])
				.then((r) => {
					const ct = ((r && r.message && r.message.channel_type) || "").trim();
					if (ct && ct !== "Interakt") {
						frappe.show_alert({
							message: __("Selected account is {0}; this mapping is intended for Interakt channels.", [ct]),
							indicator: "orange",
						});
					}
				});
		}

		if (!frm.is_new() && frm.doc.is_active) {
			frm.add_custom_button(__("Sync Contacts to Interakt"), () => sync_contacts_to_interakt(frm), {
				btn_class: "btn-primary",
			});
		}
	},
});

function sync_contacts_to_interakt(frm) {
	frappe.confirm(
		__(
			"Push patients (this Medical Department), CRM leads (this pipeline), and chat contacts on this Interakt account to Interakt?"
		),
		() => {
			frappe.call({
				method: "wa_chat_hub.api.contact_sync.sync_pipeline_map_to_interakt",
				args: { pipeline_map: frm.doc.name },
				freeze: true,
				freeze_message: __("Syncing contacts to Interakt..."),
				callback(r) {
					const result = (r.message && r.message.result) || {};
					frappe.msgprint({
						title: __("Interakt sync finished"),
						indicator: result.failed ? "orange" : "green",
						message: __("Pushed: {0}, Failed: {1}, Skipped: {2}", [
							result.pushed || 0,
							result.failed || 0,
							result.skipped || 0,
						]),
					});
				},
			});
		}
	);
}
