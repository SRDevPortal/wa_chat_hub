frappe.provide("wa_chat_hub");

wa_chat_hub.open_reference_chat = async function (doctype, name) {
    if (wa_chat_hub.reference_chat_pending) return;
    wa_chat_hub.reference_chat_pending = true;
    try {
        const response = await frappe.call({
            method: "wa_chat_hub.api.reference_chat.find_chats",
            args: { reference_doctype: doctype, reference_name: name },
            freeze: true,
            freeze_message: __("Finding WhatsApp conversation..."),
        });
        const result = response.message || {};
        const conversations = result.conversations || [];
        const open = (name) => {
            // The WhatsApp workspace opens this page; a workspace cannot select a thread.
            frappe.route_options = { conversation: String(name) };
            frappe.set_route("wa-chat-hub");
        };
        if (!conversations.length) {
            frappe.msgprint({ title: __("WhatsApp Chat"), message: result.message, indicator: "orange" });
        } else if (conversations.length === 1 && !result.has_more) {
            open(conversations[0].name);
        } else {
            const labels = new Map(conversations.map(row => [
                `${row.name} · ${row.channel_account} · ${row.status} · ${row.last_message_time || ""}`,
                row.name,
            ]));
            const dialog = new frappe.ui.Dialog({
                title: __("Choose WhatsApp Chat"),
                fields: [{ fieldname: "conversation", fieldtype: "Select", label: __("Conversation"),
                    options: [...labels.keys()], reqd: 1,
                    description: result.has_more ? __("Showing the latest 50 matches. Open WhatsApp to search older chats.") : "" }],
                primary_action_label: __("Open Chat"),
                primary_action(values) {
                    dialog.hide();
                    open(labels.get(values.conversation));
                },
            });
            dialog.show();
        }
    } finally {
        wa_chat_hub.reference_chat_pending = false;
    }
};
