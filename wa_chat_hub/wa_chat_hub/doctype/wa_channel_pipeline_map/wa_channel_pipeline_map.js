frappe.ui.form.on("WA Channel Pipeline Map", {
	refresh(frm) {
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
	},
});
