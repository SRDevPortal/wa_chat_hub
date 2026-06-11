(function () {
	let attempts = 0;

	function isRemoteUrl(value) {
		return /^https?:\/\//i.test(value || "");
	}

	function normalizeRemoteUrl(value) {
		if (!isRemoteUrl(value)) {
			return value;
		}
		let url = value.replace(/#/g, "%23");
		for (let i = 0; i < 2 && /%25[0-9a-f]{2}/i.test(url); i += 1) {
			try {
				url = decodeURI(url);
			} catch (error) {
				break;
			}
		}
		return url;
	}

	function patchRenderedLinks(root) {
		if (!root || !root.querySelectorAll) {
			return;
		}
		root.querySelectorAll('a[href^="http://"], a[href^="https://"]').forEach((link) => {
			const rawHref = link.getAttribute("href") || "";
			const normalized = normalizeRemoteUrl(rawHref);
			if (normalized && normalized !== rawHref) {
				link.setAttribute("href", normalized);
			}
		});
	}

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
			if (isRemoteUrl(fileUrl)) {
				return normalizeRemoteUrl(fileUrl);
			}
			return originalGetFileUrl.call(this, attachment);
		};
		proto.__wa_chat_hub_remote_url_patch = true;

		if (frappe.router && frappe.router.on) {
			frappe.router.on("change", () => setTimeout(() => patchRenderedLinks(document), 500));
		}
		if (window.MutationObserver) {
			new MutationObserver((mutations) => {
				mutations.forEach((mutation) => {
					mutation.addedNodes.forEach((node) => patchRenderedLinks(node));
				});
			}).observe(document.body, { childList: true, subtree: true });
		}
		patchRenderedLinks(document);
	}

	applyPatch();
})();
