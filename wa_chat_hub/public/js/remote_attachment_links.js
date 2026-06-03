(function () {
	let attempts = 0;

	function applyPatch() {
		attempts += 1;
		if (!window.frappe || !frappe.ui || !frappe.ui.form || !frappe.ui.form.Attachments) {
			if (attempts < 40) {
				setTimeout(applyPatch, 250);
			}
			return;
		}

		const proto = frappe.ui.form.Attachments.prototype;
		if (!proto || proto.__wa_chat_hub_remote_url_patch) {
			return;
		}

		const originalGetFileUrl = proto.get_file_url;
		proto.get_file_url = function (attachment) {
			const fileUrl = (attachment && attachment.file_url) || "";
			if (/^https?:\/\//i.test(fileUrl)) {
				return fileUrl.replace(/#/g, "%23");
			}
			return originalGetFileUrl.call(this, attachment);
		};
		proto.__wa_chat_hub_remote_url_patch = true;
	}

	applyPatch();
})();
