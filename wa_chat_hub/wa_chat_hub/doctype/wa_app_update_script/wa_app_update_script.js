frappe.ui.form.on("WA App Update Script", {
    refresh(frm) {
        if (frm.is_new()) {
            return;
        }

        if (frm.doc.enabled) {
            frm.add_custom_button(__("Run Again"), () => run_update_action(frm, "run_update"), __("Update Actions"));
            frm.add_custom_button(__("Disable Update"), () => run_update_action(frm, "disable_update"), __("Update Actions"));
        } else {
            frm.add_custom_button(__("Enable Update"), () => run_update_action(frm, "enable_update"), __("Update Actions"));
        }

        frm.add_custom_button(__("View Logs"), () => {
            frappe.set_route("List", "WA App Update Log", { update_script: frm.doc.name });
        });

        frm.add_custom_button(__("Delete Update"), () => {
            frappe.confirm(
                __("Delete this update definition? Its execution logs will remain."),
                () => run_update_action(frm, "delete_update", true)
            );
        });
    },
});

function run_update_action(frm, action, route_after_delete = false) {
    frappe.call({
        method: `wa_chat_hub.app_update.${action}`,
        args: { name: frm.doc.name },
        freeze: true,
        freeze_message: __("Executing app update..."),
        callback(r) {
            if (r.message && r.message.message) {
                frappe.show_alert({ message: r.message.message, indicator: "green" });
            }
            if (route_after_delete) {
                frappe.set_route("List", "WA App Update Script");
            } else {
                frm.reload_doc();
            }
        },
    });
}
