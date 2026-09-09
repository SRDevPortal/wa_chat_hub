frappe.listview_settings["Lead"] = frappe.listview_settings["Lead"] || {};

(function () {
    const base = frappe.listview_settings["Lead"];
    const priorOnload = base.onload;

    base.add_fields = Array.from(
        new Set([...(base.add_fields || []), "mobile_no", "phone", "lead_score", "lead_lan", "lead_temperature"])
    );

    base.onload = function (listview) {
        if (typeof priorOnload === "function") {
            priorOnload(listview);
        }
        listview.page.add_action_item(__("Open Chat"), function () {
            const names = listview.get_checked_items(true);
            if (!names.length) {
                frappe.msgprint(__("Select one Lead row first."));
                return;
            }
            const leadName = names[0];
            openChatForLead(leadName);
        });
    };

    base.button = {
        show: function () {
            return true;
        },
        get_label: function () {
            return __("Open Chat");
        },
        get_description: function () {
            return __("Open linked WA conversation");
        },
        action: function (doc) {
            openChatForLead(doc.name, doc.mobile_no || doc.phone || "");
        },
    };

    function openChatForLead(leadName, phone) {
        frappe.call({
            method: "wa_chat_hub.api.chat.resolve_chat_for_reference",
            args: {
                reference_doctype: "Lead",
                reference_name: leadName,
                phone_number: phone || "",
            },
            callback: function (r) {
                const conversation = (((r || {}).message || {}).result || {}).conversation;
                if (conversation) {
                    frappe.route_options = { conversation };
                    frappe.set_route("wa-chat-hub");
                    return;
                }
                frappe.route_options = {
                    linked_lead: leadName,
                };
                frappe.set_route("List", "Chat Conversation");
            },
        });
    }
})();
