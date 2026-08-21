(function () {
	const CHAT_BUTTON_CLASS = "wa-lead-chat-button";
	let activeDialog = null;
	let activeConversation = null;
	let realtimeBound = false;

	frappe.ui.form.on("Lead", {
		refresh(frm) {
			if (frm.is_new()) {
				return;
			}
			install_styles();
			install_chat_button(frm);
		},
	});

	function install_chat_button(frm) {
		if (frm.__wa_lead_chat_button_installed) {
			return;
		}
		frm.__wa_lead_chat_button_installed = true;

		const $button = frm.page.add_inner_button(__("WhatsApp Chat"), function () {
			open_lead_chat(frm);
		});
		$button.addClass(CHAT_BUTTON_CLASS);
		place_button_before_actions(frm, $button);
	}

	function place_button_before_actions(frm, $button) {
		const $actions = frm.page.wrapper.find(".page-actions");
		const $target = $actions.find(".custom-actions, .actions-btn-group").first();
		if ($target.length && $button && $button.length) {
			$button.insertBefore($target);
		}
	}

	function open_lead_chat(frm) {
		frappe.call({
			method: "wa_chat_hub.api.chat.get_conversation_for_reference",
			args: {
				reference_doctype: "Lead",
				reference_name: frm.doc.name,
			},
			freeze: true,
			freeze_message: __("Finding WhatsApp chat..."),
			callback(r) {
				const data = r.message || {};
				if (!data.success || !data.conversation) {
					frappe.msgprint({
						title: __("WhatsApp Chat"),
						message: data.message || __("No WhatsApp conversation found for this Lead."),
						indicator: "orange",
					});
					return;
				}
				show_chat_sidebar(frm, data.conversation);
			},
		});
	}

	function show_chat_sidebar(frm, conversation) {
		close_active_sidebar();
		activeConversation = conversation;
		const $wrapper = $(`
			<div class="wa-lead-chat-sidebar-wrap">
				<div class="wa-lead-chat-sidebar" role="dialog" aria-label="${escape_attr(__("WhatsApp Chat"))}">
					${render_shell(frm)}
				</div>
			</div>
		`).appendTo("body");
		activeDialog = { $wrapper };
		$("body").addClass("wa-lead-chat-sidebar-open");
		bind_dialog_events(activeDialog, conversation);
		bind_realtime_once();
		load_messages(conversation);
		setTimeout(() => $wrapper.addClass("visible"), 20);
	}

	function render_shell(frm) {
		const phone = frm.doc.mobile_no || frm.doc.phone || frm.doc.whatsapp_no || "";
		return `
			<div class="wa-lead-chat-shell">
				<div class="wa-lead-chat-header">
					<div>
						<div class="wa-lead-chat-title">${escape_html(frm.doc.lead_name || frm.doc.first_name || frm.doc.name)}</div>
						<div class="wa-lead-chat-subtitle">${escape_html(phone)}</div>
					</div>
					<div class="wa-lead-chat-header-actions">
						<button class="btn btn-xs btn-default wa-lead-chat-refresh" type="button">${__("Refresh")}</button>
						<button class="btn btn-xs btn-default wa-lead-chat-close" type="button" aria-label="${escape_attr(__("Close"))}">&times;</button>
					</div>
				</div>
				<div class="wa-lead-chat-messages">
					<div class="wa-lead-chat-empty">${__("Loading messages...")}</div>
				</div>
				<div class="wa-lead-chat-composer">
					<textarea class="form-control wa-lead-chat-input" rows="2" placeholder="${escape_html(__("Type a WhatsApp reply..."))}"></textarea>
					<button class="btn btn-primary wa-lead-chat-send" type="button">${__("Send")}</button>
				</div>
			</div>
		`;
	}

	function bind_dialog_events(dialog, conversation) {
		const $wrapper = dialog.$wrapper;
		$wrapper.find(".wa-lead-chat-close").on("click", function () {
			close_active_sidebar();
		});
		$wrapper.find(".wa-lead-chat-refresh").on("click", function () {
			load_messages(conversation);
		});
		$wrapper.find(".wa-lead-chat-send").on("click", function () {
			send_reply(dialog, conversation);
		});
		$wrapper.find(".wa-lead-chat-input").on("keydown", function (event) {
			if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
				event.preventDefault();
				send_reply(dialog, conversation);
			}
		});
	}

	function close_active_sidebar() {
		if (!activeDialog || !activeDialog.$wrapper) {
			return;
		}
		const $wrapper = activeDialog.$wrapper;
		activeDialog = null;
		activeConversation = null;
		$("body").removeClass("wa-lead-chat-sidebar-open");
		$wrapper.removeClass("visible");
		setTimeout(() => $wrapper.remove(), 180);
	}

	function load_messages(conversation) {
		const dialog = activeDialog;
		if (!dialog || String(conversation) !== String(activeConversation)) {
			return;
		}
		const $list = dialog.$wrapper.find(".wa-lead-chat-messages");
		$list.html(`<div class="wa-lead-chat-empty">${__("Loading messages...")}</div>`);
		frappe.call({
			method: "wa_chat_hub.api.chat.get_messages",
			args: { conversation, limit: 200 },
			callback(r) {
				const rows = ((r.message || {}).result || []);
				render_messages(dialog, rows);
			},
		});
	}

	function render_messages(dialog, rows) {
		const $list = dialog.$wrapper.find(".wa-lead-chat-messages");
		if (!rows.length) {
			$list.html(`<div class="wa-lead-chat-empty">${__("No WhatsApp messages yet.")}</div>`);
			return;
		}
		$list.html(rows.map(render_message).join(""));
		$list.scrollTop($list.prop("scrollHeight"));
	}

	function render_message(row) {
		const direction = row.direction === "Outbound" ? "outbound" : "inbound";
		return `
			<div class="wa-lead-chat-row ${direction}">
				<div class="wa-lead-chat-bubble">
					${render_message_content(row)}
					<div class="wa-lead-chat-meta">
						${escape_html(format_time(row.creation))}
						${row.direction === "Outbound" ? `<span>${escape_html(row.delivery_status || "Pending")}</span>` : ""}
					</div>
				</div>
			</div>
		`;
	}

	function render_message_content(row) {
		const type = row.content_type || "Text";
		const raw_body = normalize_message_body(row.body || "");
		const body = escape_html(raw_body);
		const media_url = row.media_proxy_url || row.attachment_url || row.media_url || "";
		if (type === "Image" && media_url) {
			return `
				<a href="${escape_attr(media_url)}" target="_blank" rel="noopener">
					<img class="wa-lead-chat-image" src="${escape_attr(media_url)}" alt="${escape_attr(body || __("Image"))}">
				</a>
				${body ? `<div class="wa-lead-chat-text">${body}</div>` : ""}
			`;
		}
		if (media_url && type !== "Text") {
			const label = row.attachment_file_name || raw_body || type;
			return `
				<a class="wa-lead-chat-attachment" href="${escape_attr(media_url)}" target="_blank" rel="noopener">
					${escape_html(label)}
				</a>
				${body && body !== escape_html(label) ? `<div class="wa-lead-chat-text">${body}</div>` : ""}
			`;
		}
		return `<div class="wa-lead-chat-text">${body || escape_html(type)}</div>`;
	}

	function send_reply(dialog, conversation) {
		const $input = dialog.$wrapper.find(".wa-lead-chat-input");
		const $button = dialog.$wrapper.find(".wa-lead-chat-send");
		const body = String($input.val() || "").trim();
		if (!body) {
			return;
		}
		$button.prop("disabled", true);
		frappe.call({
			method: "wa_chat_hub.api.runtime.send_reply",
			type: "POST",
			args: {
				conversation,
				body,
				content_type: "Text",
			},
			callback(r) {
				if ((r.message || {}).success) {
					$input.val("");
					load_messages(conversation);
				}
				$button.prop("disabled", false);
			},
			error() {
				$button.prop("disabled", false);
			},
		});
	}

	function bind_realtime_once() {
		if (realtimeBound || !frappe.realtime) {
			return;
		}
		realtimeBound = true;
		frappe.realtime.on("wa_chat_new_message", function (data) {
			if (data && activeDialog && String(data.conversation || "") === String(activeConversation || "")) {
				load_messages(activeConversation);
			}
		});
		frappe.realtime.on("wa_chat_conversation_updated", function (data) {
			if (data && activeDialog && String(data.conversation || "") === String(activeConversation || "")) {
				load_messages(activeConversation);
			}
		});
	}

	function format_time(value) {
		if (!value) {
			return "";
		}
		const date = new Date(value);
		if (Number.isNaN(date.getTime())) {
			return String(value).slice(0, 16);
		}
		return date.toLocaleString([], {
			day: "2-digit",
			month: "short",
			hour: "2-digit",
			minute: "2-digit",
		});
	}

	function escape_html(value) {
		const div = document.createElement("div");
		div.textContent = value == null ? "" : String(value);
		return div.innerHTML;
	}

	function escape_attr(value) {
		return escape_html(value).replace(/"/g, "&quot;");
	}

	function normalize_message_body(value) {
		return String(value || "")
			.replace(/\r\n/g, "\n")
			.replace(/\u00a0/g, " ")
			.split("\n")
			.map((line) => line.trimEnd())
			.join("\n")
			.replace(/\n{3,}/g, "\n\n")
			.trim();
	}

	function install_styles() {
		if (document.getElementById("wa-lead-chat-popup-style")) {
			return;
		}
		const style = document.createElement("style");
		style.id = "wa-lead-chat-popup-style";
		style.textContent = `
			.${CHAT_BUTTON_CLASS} {
				margin-right: 8px;
			}
			.wa-lead-chat-sidebar-wrap {
				position: fixed;
				top: 0;
				right: 0;
				bottom: 0;
				width: min(480px, 34vw);
				min-width: 390px;
				z-index: 1040;
				pointer-events: none;
				transform: translateX(18px);
				opacity: 0;
				transition: transform 160ms ease, opacity 160ms ease;
			}
			.wa-lead-chat-sidebar-wrap.visible {
				transform: translateX(0);
				opacity: 1;
			}
			.wa-lead-chat-sidebar {
				height: 100%;
				pointer-events: auto;
				background: #fff;
				border-left: 1px solid #d1d5db;
				box-shadow: -12px 0 28px rgba(15, 23, 42, 0.18);
			}
			.wa-lead-chat-shell {
				display: flex;
				flex-direction: column;
				height: 100vh;
				min-height: 0;
				background: #f3f4f6;
			}
			.wa-lead-chat-header {
				display: flex;
				align-items: center;
				justify-content: space-between;
				gap: 12px;
				padding: 14px 18px;
				border-bottom: 1px solid #e5e7eb;
				background: #fff;
			}
			.wa-lead-chat-header-actions {
				display: flex;
				align-items: center;
				gap: 8px;
				flex-shrink: 0;
			}
			.wa-lead-chat-close {
				width: 28px;
				height: 28px;
				padding: 0;
				font-size: 20px;
				line-height: 20px;
			}
			.wa-lead-chat-title {
				font-size: 15px;
				font-weight: 650;
				color: #111827;
			}
			.wa-lead-chat-subtitle {
				font-size: 12px;
				color: #6b7280;
				margin-top: 2px;
			}
			.wa-lead-chat-messages {
				flex: 1;
				overflow: auto;
				padding: 12px 10px 14px;
				background: #ece5dd;
			}
			.wa-lead-chat-empty {
				color: #6b7280;
				text-align: center;
				padding: 30px 12px;
			}
			.wa-lead-chat-row {
				display: flex;
				margin: 5px 0;
			}
			.wa-lead-chat-row.inbound {
				justify-content: flex-start;
			}
			.wa-lead-chat-row.outbound {
				justify-content: flex-end;
			}
			.wa-lead-chat-bubble {
				width: fit-content;
				min-width: 0;
				max-width: min(360px, 78%);
				padding: 4px 5px 3px;
				border-radius: 8px;
				background: #fff;
				border: 1px solid rgba(17, 24, 39, 0.08);
				box-shadow: 0 1px 1px rgba(0, 0, 0, 0.08);
				overflow-wrap: anywhere;
				color: #111827;
				align-self: flex-start;
			}
			.wa-lead-chat-row.outbound .wa-lead-chat-bubble {
				background: #d9fdd3;
				border-color: #b7efc7;
				align-self: flex-end;
			}
			.wa-lead-chat-text {
				max-width: 336px;
				padding: 2px 6px 0;
				font-size: 13.5px;
				line-height: 1.35;
				white-space: pre-wrap;
				overflow-wrap: anywhere;
			}
			.wa-lead-chat-meta {
				display: flex;
				align-items: center;
				justify-content: flex-end;
				gap: 5px;
				min-height: 13px;
				padding: 1px 5px 0;
				font-size: 10.5px;
				line-height: 1;
				color: #667781;
			}
			.wa-lead-chat-image {
				display: block;
				max-width: 280px;
				max-height: 260px;
				border-radius: 6px;
				margin-bottom: 6px;
				object-fit: cover;
			}
			.wa-lead-chat-attachment {
				display: inline-flex;
				max-width: 100%;
				padding: 8px 10px;
				border-radius: 6px;
				background: rgba(17, 24, 39, 0.06);
				color: #1f2937;
				text-decoration: none;
			}
			.wa-lead-chat-composer {
				display: flex;
				align-items: flex-end;
				gap: 10px;
				padding: 8px 10px;
				border-top: 1px solid #e5e7eb;
				background: #f9fafb;
			}
			.wa-lead-chat-input {
				resize: none;
				min-height: 38px;
				max-height: 96px;
			}
			.wa-lead-chat-send {
				min-width: 82px;
			}
			body.wa-lead-chat-sidebar-open .layout-main-section-wrapper,
			body.wa-lead-chat-sidebar-open .form-layout {
				max-width: calc(100vw - min(500px, 36vw));
			}
			body.wa-lead-chat-sidebar-open .page-actions {
				padding-right: min(500px, 36vw);
			}
			@media (max-width: 1100px) {
				.wa-lead-chat-sidebar-wrap {
					width: min(440px, 42vw);
					min-width: 360px;
				}
				body.wa-lead-chat-sidebar-open .layout-main-section-wrapper,
				body.wa-lead-chat-sidebar-open .form-layout,
				body.wa-lead-chat-sidebar-open .page-actions {
					max-width: none;
					padding-right: 0;
				}
			}
			@media (max-width: 640px) {
				.wa-lead-chat-sidebar-wrap {
					width: min(420px, 94vw);
					min-width: 0;
				}
				.wa-lead-chat-shell {
					height: 100vh;
				}
				.wa-lead-chat-bubble {
					max-width: 88%;
				}
			}
		`;
		document.head.appendChild(style);
	}
})();
