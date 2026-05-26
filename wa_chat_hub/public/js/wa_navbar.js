frappe.provide("wa_chat_hub.notifications");
frappe.provide("wa_chat_hub.realtime");

$(document).ready(function() {
    wa_chat_hub.realtime.patch_unsaved_doc_subscriptions();
    wa_chat_hub.notifications.setup();
    frappe.router.on('change', function() {
        setTimeout(function() {
            wa_chat_hub.realtime.patch_unsaved_doc_subscriptions();
            wa_chat_hub.notifications.insert_icon();
        }, 100);
    });
});

wa_chat_hub.realtime = {
    patch_unsaved_doc_subscriptions: function() {
        if (!frappe.realtime || frappe.realtime.__wa_skip_unsaved_docs_patched) {
            return;
        }

        ["doc_subscribe", "doc_open", "doc_close", "doc_unsubscribe"].forEach(function(method) {
            if (typeof frappe.realtime[method] !== "function") {
                return;
            }

            const original = frappe.realtime[method].bind(frappe.realtime);
            frappe.realtime[method] = function(doctype, docname) {
                if (wa_chat_hub.realtime.is_unsaved_docname(docname)) {
                    return;
                }
                return original(doctype, docname);
            };
        });

        frappe.realtime.__wa_skip_unsaved_docs_patched = true;
    },

    is_unsaved_docname: function(docname) {
        return typeof docname === "string" && docname.indexOf("new-") === 0;
    }
};

wa_chat_hub.notifications = {
    setup: function() {
        if (!frappe.ui.toolbar) {
            setTimeout(wa_chat_hub.notifications.setup, 500);
            return;
        }

        this.insert_icon();
        this.bind_events();
        this.refresh_count();
    },

    insert_icon: function() {
        if ($('.custom-wa-dropdown').length > 0) {
            wa_chat_hub.notifications.refresh_count();
            return;
        } // Prevent duplicates gracefully
        
        // Appends to the right side of the navbar
        let html = `
            <li class="nav-item dropdown custom-wa-dropdown d-none d-md-flex">
                <a class="nav-link" data-toggle="dropdown" href="#" role="button" aria-haspopup="true" aria-expanded="false" title="WhatsApp Messages">
                    <span style="position:relative;">
                        <i class="fa fa-whatsapp" style="font-size: 18px; color: #25D366; vertical-align: middle;"></i>
                        <span class="badge badge-danger wa-unread-badge" style="position: absolute; top: -7px; right: -12px; min-width: 16px; height: 16px; padding: 2px 4px; border-radius: 10px; font-size: 10px; line-height: 12px; display: none;"></span>
                    </span>
                </a>
                <div class="dropdown-menu dropdown-menu-right wa-dropdown-box" style="width: 340px; max-height: 400px; overflow-y: auto; padding: 0; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">
                    <div class="dropdown-header d-flex justify-content-between align-items-center" style="background: #f8f9fa; border-bottom: 1px solid #e2e2e2; padding: 12px;">
                        <strong style="color: #333;"><i class="fa fa-whatsapp"></i> WhatsApp Live Desk</strong>
                        <a href="/app/wa-chat-hub" class="text-primary" style="font-size: 12px;"><i class="fa fa-external-link"></i> Open Hub</a>
                    </div>
                    <div class="wa-notifications-list">
                        <!-- Loaded dynamically -->
                    </div>
                </div>
            </li>
        `;
        
        // Find Frappe's native notification icon to inject exactly next to it on the right
        let notification_bell = $('#navbar-notification').closest('li');
        if (notification_bell.length > 0) {
            notification_bell.before(html);
        } else {
            $('header .navbar-nav').last().prepend(html);
        }
        
        // On dropdown open, fetch limits
        $('.custom-wa-dropdown').on('show.bs.dropdown', function () {
            wa_chat_hub.notifications.load_recent();
            wa_chat_hub.notifications.refresh_count();
        });
    },

    load_recent: function() {
        let list_container = $('.wa-notifications-list');
        list_container.html('<div class="p-4 text-center text-muted"><i class="fa fa-spinner fa-spin fa-2x"></i></div>');
        
        frappe.call({
            method: "wa_chat_hub.api.notifications.get_recent_messages",
            callback: function(r) {
                if (r.message && r.message.length > 0) {
                    let items_html = r.message.map(m => {
                        let short_body = m.body ? m.body.substring(0, 50) + (m.body.length > 50 ? '...' : '') : '[Media]';
                        return `
                            <a class="dropdown-item d-flex flex-column border-bottom" href="/app/wa-chat-hub" style="padding: 12px; white-space: normal;">
                                <div class="d-flex justify-content-between w-100 mb-1">
                                    <strong style="font-size: 13px; color: #1f272e;">${m.sender_name}</strong>
                                    <small class="text-muted" style="font-size: 11px;">${frappe.datetime.comment_when(m.creation)}</small>
                                </div>
                                <div class="text-muted" style="font-size: 12px;">${short_body}</div>
                            </a>
                        `;
                    }).join("");
                    list_container.html(items_html);
                } else {
                    list_container.html('<div class="p-4 text-center text-muted">No recent messages.</div>');
                }
            }
        });
    },
    
    refresh_count: function() {
        frappe.call({
            method: "wa_chat_hub.api.notifications.get_unread_count",
            callback: function(r) {
                wa_chat_hub.notifications.render_count(r.message || 0);
            }
        });
    },

    render_count: function(count) {
        count = cint(count || 0);
        const badge = $('.wa-unread-badge');
        if (!badge.length) return;

        if (count > 0) {
            badge.show().text(count > 99 ? "99+" : count);
        } else {
            badge.hide().text("");
        }
    },

    bind_events: function() {
        frappe.realtime.on("wa_chat_new_message", function(data) {
            if (data.message && data.message.direction === "Inbound") {
                // If dropdown is open, refresh it. Else, increment counter.
                if ($('.custom-wa-dropdown').hasClass('show')) {
                    wa_chat_hub.notifications.load_recent();
                }
                wa_chat_hub.notifications.refresh_count();
                frappe.utils.play_sound("notification");
            }
        });
    }
}
