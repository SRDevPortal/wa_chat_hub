(function () {
	const doctypes = ["CRM Lead", "Lead", "Customer"];
	const referenceChatStatusEnabled = false;
	const statusDoctypes = referenceChatStatusEnabled ? ["CRM Lead"] : [];

	doctypes.forEach((doctype) => setup_reference_chat_button(doctype));

	function setup_reference_chat_button(doctype) {
		const existingSettings = frappe.listview_settings[doctype] || {};
		const existingOnload = existingSettings.onload;

		frappe.listview_settings[doctype] = {
			...existingSettings,
			onload(listview) {
				if (typeof existingOnload === "function") {
					existingOnload(listview);
				}
				if (statusDoctypes.includes(listview.doctype)) {
					install_reference_chat_status(listview);
				}
			},
			button: {
				show() {
					return true;
				},
				get_label() {
					return __("Open Chat");
				},
				get_description() {
					return __("Open WhatsApp conversation");
				},
				action(doc) {
					open_reference_chat(doctype, doc.name);
				},
			},
		};
	}

	function install_reference_chat_status(listview) {
		if (listview.__wa_reference_chat_status_installed) {
			return;
		}

		listview.__wa_reference_chat_status_installed = true;
		install_style();

		const originalRender = listview.render.bind(listview);
		listview.render = function () {
			originalRender();
			schedule_status_refresh(listview);
		};

		if (frappe.realtime && !listview.__wa_reference_chat_realtime_installed) {
			listview.__wa_reference_chat_realtime_installed = true;
			frappe.realtime.on("wa_chat_new_message", () => schedule_status_refresh(listview));
			frappe.realtime.on("wa_chat_conversation_updated", () => schedule_status_refresh(listview));
		}

		schedule_status_refresh(listview);
	}

	function schedule_status_refresh(listview) {
		if (listview.__wa_reference_chat_status_disabled) {
			return;
		}
		clearTimeout(listview.__wa_reference_chat_status_timer);
		listview.__wa_reference_chat_status_timer = setTimeout(() => {
			refresh_chat_statuses(listview);
		}, 750);
	}

	function refresh_chat_statuses(listview) {
		if (listview.__wa_reference_chat_status_disabled) {
			return;
		}
		if (listview.__wa_reference_chat_status_in_flight) {
			listview.__wa_reference_chat_status_pending = true;
			return;
		}

		const names = (listview.data || [])
			.map((doc) => doc.name)
			.filter((name) => name && !String(name).startsWith("new-"));
		if (!names.length) {
			return;
		}

		listview.__wa_reference_chat_status_in_flight = true;
		frappe.call({
			method: "wa_chat_hub.api.chat.get_reference_chat_statuses",
			args: {
				reference_doctype: listview.doctype,
				reference_names: names,
			},
			callback(r) {
				const statuses = (r.message && r.message.result) || {};
				apply_chat_statuses(listview, statuses);
				promote_unread_rows(listview, statuses);
			},
			error(r) {
				if (r && [401, 403].includes(cint(r.status))) {
					listview.__wa_reference_chat_status_disabled = true;
					return;
				}
			},
			always() {
				listview.__wa_reference_chat_status_in_flight = false;
				if (listview.__wa_reference_chat_status_pending) {
					listview.__wa_reference_chat_status_pending = false;
					schedule_status_refresh(listview);
				}
			},
		});
	}

	function apply_chat_statuses(listview, statuses) {
		(listview.data || []).forEach((doc) => {
			const status = statuses[doc.name] || {};
			const $button = listview.$result.find(`.btn-action[data-name="${escape_selector(doc.name)}"]`);

			if (!$button.length) {
				return;
			}

			const unreadCount = cint(status.unread_count);
			const conversationCount = cint(status.conversation_count);
			const hasUnread = unreadCount > 0;
			const countLabel = conversationCount ? ` (${conversationCount})` : "";
			const title = hasUnread
				? __("{0} unread WhatsApp message(s)", [unreadCount])
				: conversationCount
					? __("{0} WhatsApp conversation(s)", [conversationCount])
					: __("Open WhatsApp conversation");

			$button
				.text(__("Open Chat") + countLabel)
				.attr("title", title)
				.toggleClass("wa-reference-chat-unread", hasUnread)
				.toggleClass("wa-reference-chat-read", !hasUnread);
		});
	}

	function promote_unread_rows(listview, statuses) {
		const rows = [];

		listview.$result.find(".list-row-container").each(function (index) {
			const $row = $(this);
			const name = $row.find(".btn-action[data-name]").attr("data-name");
			const status = statuses[name] || {};
			rows.push({
				$row,
				index,
				unread_count: cint(status.unread_count),
				last_message_time: status.last_message_time || "",
			});
		});

		rows.sort((a, b) => {
			if (!!b.unread_count !== !!a.unread_count) {
				return b.unread_count - a.unread_count;
			}
			if (b.unread_count !== a.unread_count) {
				return b.unread_count - a.unread_count;
			}
			if (a.last_message_time !== b.last_message_time) {
				return String(b.last_message_time).localeCompare(String(a.last_message_time));
			}
			return a.index - b.index;
		});

		rows.forEach((row) => listview.$result.append(row.$row));
	}

	function install_style() {
		if (document.getElementById("wa-reference-chat-list-style")) {
			return;
		}

		$(`<style id="wa-reference-chat-list-style">
			.btn.wa-reference-chat-unread {
				background: #16a34a;
				border-color: #15803d;
				color: #fff;
				box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.55);
				animation: wa-reference-chat-pulse 1.35s ease-in-out infinite;
			}
			.btn.wa-reference-chat-unread:hover,
			.btn.wa-reference-chat-unread:focus {
				background: #15803d;
				border-color: #166534;
				color: #fff;
			}
			.btn.wa-reference-chat-read {
				background: var(--control-bg);
				border-color: var(--border-color);
				color: var(--text-color);
				animation: none;
			}
			@keyframes wa-reference-chat-pulse {
				0% { box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.55); }
				70% { box-shadow: 0 0 0 8px rgba(22, 163, 74, 0); }
				100% { box-shadow: 0 0 0 0 rgba(22, 163, 74, 0); }
			}
		</style>`).appendTo("head");
	}

	function escape_selector(value) {
		if (window.CSS && CSS.escape) {
			return CSS.escape(value);
		}
		return String(value).replace(/["\\]/g, "\\$&");
	}

	function open_reference_chat(reference_doctype, reference_name) {
		frappe.call({
			method: "wa_chat_hub.api.chat.get_conversation_for_reference",
			args: {
				reference_doctype,
				reference_name,
			},
			freeze: true,
			freeze_message: __("Finding WhatsApp conversation..."),
			callback(r) {
				const result = r.message || {};
				if (!result.success || !result.conversation) {
					frappe.msgprint({
						title: __("No WhatsApp Conversation"),
						indicator: "orange",
						message: result.message || __("No WhatsApp conversation found for this record."),
					});
					return;
				}

				frappe.route_options = {
					conversation: result.conversation,
				};
				frappe.set_route("wa-chat-hub");
			},
		});
	}
})();
