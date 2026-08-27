frappe.listview_settings["Chat Conversation"] = {
	add_fields: [
		"channel_account",
		"contact",
		"assigned_to",
		"status",
		"priority",
		"lead_score",
		"lead_lan",
		"lead_temperature",
		"unread_count",
		"last_message_preview",
	],
	filters: [["status", "!=", "Closed"]],
	get_indicator: function (doc) {
		if (doc.unread_count > 0) {
			return [__("Unread"), "orange", "unread_count,>,0"];
		}
		if (doc.status === "Open") {
			return [__("Open"), "green", "status,=,Open"];
		}
		if (doc.status === "Pending") {
			return [__("Pending"), "blue", "status,=,Pending"];
		}
		if (doc.status === "Resolved") {
			return [__("Resolved"), "purple", "status,=,Resolved"];
		}
		if (doc.status === "Closed") {
			return [__("Closed"), "darkgrey", "status,=,Closed"];
		}
		return [__(doc.status || "Unknown"), "grey", "status,=," + (doc.status || "")];
	},
	onload: function (listview) {
		add_queue_buttons(listview);
		add_bulk_actions(listview);
	},
	button: {
		show: function () {
			return true;
		},
		get_label: function () {
			return __("Open Chat");
		},
		get_description: function () {
			return __("Open this conversation in WA Chat Hub");
		},
		action: function (doc) {
			open_chat(doc.name);
		},
	},
};

function add_queue_buttons(listview) {
	listview.page.add_inner_button(__("Open Chat Hub"), function () {
		frappe.set_route("wa-chat-hub");
	});

	listview.page.add_inner_button(__("My Queue"), function () {
		frappe.route_options = {
			assigned_to: frappe.session.user,
			status: ["!=", "Closed"],
		};
		frappe.set_route("List", "Chat Conversation");
	});

	listview.page.add_inner_button(__("Unassigned"), function () {
		frappe.route_options = {
			assigned_to: ["is", "not set"],
			status: ["!=", "Closed"],
		};
		frappe.set_route("List", "Chat Conversation");
	});
}

function add_bulk_actions(listview) {
	listview.page.add_action_item(__("Assign to User"), function () {
		const names = get_selected_names(listview);
		if (!names.length) return;

		frappe.prompt(
			{
				fieldname: "user",
				fieldtype: "Link",
				options: "User",
				label: __("User"),
				reqd: 1,
			},
			(values) => bulk_assign(listview, names, values.user),
			__("Assign Conversations"),
			__("Assign")
		);
	});

	listview.page.add_action_item(__("Assign to Me"), function () {
		const names = get_selected_names(listview);
		if (names.length) {
			bulk_assign(listview, names, frappe.session.user);
		}
	});

	listview.page.add_action_item(__("Clear Assignment"), function () {
		const names = get_selected_names(listview);
		if (names.length) {
			bulk_assign(listview, names, null);
		}
	});

	["Open", "Pending", "Resolved", "Closed"].forEach((status) => {
		listview.page.add_action_item(__("Set Status: {0}", [status]), function () {
			const names = get_selected_names(listview);
			if (names.length) {
				bulk_update(listview, names, "status", status);
			}
		});
	});

	["Low", "Medium", "High", "Urgent"].forEach((priority) => {
		listview.page.add_action_item(__("Set Priority: {0}", [priority]), function () {
			const names = get_selected_names(listview);
			if (names.length) {
				bulk_update(listview, names, "priority", priority);
			}
		});
	});
}

function get_selected_names(listview) {
	const names = listview.get_checked_items(true);
	if (!names.length) {
		frappe.msgprint(__("Select one or more conversations first."));
	}
	return names;
}

function bulk_assign(listview, names, user) {
	frappe.call({
		method: "wa_chat_hub.api.chat.bulk_assign",
		args: {
			conversations: names,
			user: user,
		},
		freeze: true,
		freeze_message: __("Updating assignments..."),
		callback: function () {
			listview.refresh();
		},
	});
}

function bulk_update(listview, names, fieldname, value) {
	frappe.call({
		method: "wa_chat_hub.api.chat.bulk_update",
		args: {
			conversations: names,
			fieldname: fieldname,
			value: value,
		},
		freeze: true,
		freeze_message: __("Updating conversations..."),
		callback: function () {
			listview.refresh();
		},
	});
}

function open_chat(conversation) {
	frappe.route_options = {
		conversation: conversation,
	};
	frappe.set_route("wa-chat-hub");
}
