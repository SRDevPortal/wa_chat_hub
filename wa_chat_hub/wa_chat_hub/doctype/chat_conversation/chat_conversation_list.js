frappe.listview_settings['Chat Conversation'] = {
	add_fields: ["status", "unread_count", "contact"],
	get_indicator: function (doc) {
		if (doc.status === "Open") {
			return [__("Open"), "green", "status,=,Open"];
		} else if (doc.status === "Closed") {
			return [__("Closed"), "darkgrey", "status,=,Closed"];
		} else {
			return [__(doc.status || "Unknown"), "blue", "status,=," + (doc.status || '')];
		}
	},
	onload: function(listview) {
		listview.page.add_inner_button(__("Open Chat Hub"), function() {
			frappe.set_route('wa-chat-interface');
		});
	},
	button: {
		show: function(doc) {
			return true;
		},
		get_label: function() {
			return __('Open Chat');
		},
		get_description: function(doc) {
			return __('Jump to real-time chat interface');
		},
		action: function(doc) {
			frappe.route_options = {
				conversation: doc.name
			};
			frappe.set_route('wa-chat-interface');
		}
	}
};
