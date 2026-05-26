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
    active_category: "all",
    categories: [
        { key: "all", label: "All" },
        { key: "crm_leads", label: "CRM Leads" },
        { key: "patients", label: "Patients" },
        { key: "ai_replies", label: "AI Replies" },
    ],

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
                        <a href="/app/wa-chat-hub" class="text-primary wa-open-all-hub" style="font-size: 12px;"><i class="fa fa-external-link"></i> Open Hub</a>
                    </div>
                    <div class="wa-notification-tabs" style="display:flex; gap:4px; padding:8px 8px 0; background:#fff; border-bottom:1px solid #eef0f2;">
                        ${wa_chat_hub.notifications.categories.map((tab) => `
                            <button type="button" class="btn btn-xs btn-default wa-notification-tab" data-category="${tab.key}" style="border-radius: 14px; font-size: 11px;">
                                ${tab.label} <span class="wa-tab-count" data-count-for="${tab.key}" style="display:none;"></span>
                            </button>
                        `).join("")}
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
            wa_chat_hub.notifications.active_category = wa_chat_hub.notifications.route_category();
            wa_chat_hub.notifications.render_active_tab();
            wa_chat_hub.notifications.load_recent();
            wa_chat_hub.notifications.refresh_count();
        });

        $('.custom-wa-dropdown').on('click', '.wa-notification-tab', function(e) {
            e.preventDefault();
            e.stopPropagation();
            wa_chat_hub.notifications.active_category = $(this).attr('data-category') || 'all';
            wa_chat_hub.notifications.render_active_tab();
            wa_chat_hub.notifications.load_recent();
        });

        $('.custom-wa-dropdown').on('click', '.wa-notification-row', function(e) {
            e.preventDefault();
            const conversation = $(this).attr('data-conversation');
            if (conversation) {
                frappe.route_options = frappe.route_options || {};
                frappe.route_options.conversation = conversation;
            }
            frappe.set_route('wa-chat-hub');
            $('.custom-wa-dropdown .nav-link').dropdown('hide');
        });

        $('.custom-wa-dropdown').on('click', '.wa-open-all-hub', function(e) {
            e.preventDefault();
            wa_chat_hub.notifications.clear_scope_and_open_all();
        });
    },

    load_recent: function() {
        let list_container = $('.wa-notifications-list');
        list_container.html('<div class="p-4 text-center text-muted"><i class="fa fa-spinner fa-spin fa-2x"></i></div>');
        
        frappe.call({
            method: "wa_chat_hub.api.notifications.get_recent_notifications",
            args: {
                category: wa_chat_hub.notifications.active_category || "all",
                limit: 10,
            },
            callback: function(r) {
                if (r.message && r.message.length > 0) {
                    let items_html = r.message.map(m => {
                        let short_body = wa_chat_hub.notifications.preview_text(m);
                        let sender = wa_chat_hub.notifications.escape(m.sender_name || m.phone_number || 'WhatsApp');
                        let badge = wa_chat_hub.notifications.row_badge(m);
                        return `
                            <a class="dropdown-item d-flex flex-column border-bottom wa-notification-row" href="/app/wa-chat-hub" data-conversation="${wa_chat_hub.notifications.escape(m.conversation || '')}" style="padding: 12px; white-space: normal;">
                                <div class="d-flex justify-content-between w-100 mb-1">
                                    <strong style="font-size: 13px; color: #1f272e;">${sender}</strong>
                                    <small class="text-muted" style="font-size: 11px;">${frappe.datetime.comment_when(m.creation)}</small>
                                </div>
                                <div class="d-flex justify-content-between align-items-center" style="gap:8px;">
                                    <div class="text-muted" style="font-size: 12px; overflow:hidden; text-overflow:ellipsis;">${short_body}</div>
                                    ${badge}
                                </div>
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
            method: "wa_chat_hub.api.notifications.get_notification_counts",
            callback: function(r) {
                wa_chat_hub.notifications.render_counts(r.message || {});
            }
        });
    },

    render_counts: function(counts) {
        counts = counts || {};
        wa_chat_hub.notifications.render_count(counts.all || 0);
        wa_chat_hub.notifications.categories.forEach((tab) => {
            const count = cint(counts[tab.key] || 0);
            const $count = $(`.wa-tab-count[data-count-for="${tab.key}"]`);
            if (!$count.length) return;
            $count.text(count > 99 ? "99+" : count).toggle(count > 0);
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

    render_active_tab: function() {
        const active = wa_chat_hub.notifications.active_category || "all";
        $('.wa-notification-tab').each(function() {
            const isActive = $(this).attr('data-category') === active;
            $(this).toggleClass('btn-primary', isActive).toggleClass('btn-default', !isActive);
        });
    },

    route_category: function() {
        const route = (frappe.get_route ? frappe.get_route() : []).join('/').toLowerCase();
        if (route.indexOf('crm-lead') !== -1 || route.indexOf('crm lead') !== -1) {
            return 'crm_leads';
        }
        if (route.indexOf('patient-encounter') !== -1 || route.indexOf('patient encounter') !== -1 || route.indexOf('patient') !== -1) {
            return 'patients';
        }
        return 'all';
    },

    preview_text: function(row) {
        let body = row.body || '';
        if (!body) {
            body = row.direction === 'Outbound' && row.sender_type === 'AI' ? '[AI reply]' : '[Media]';
        }
        if (row.direction === 'Outbound' && row.sender_type === 'AI') {
            body = `AI: ${body}`;
        }
        const shortBody = body.substring(0, 58) + (body.length > 58 ? '...' : '');
        return wa_chat_hub.notifications.escape(shortBody);
    },

    row_badge: function(row) {
        if (row.direction === 'Outbound' && row.sender_type === 'AI') {
            return '<span class="badge badge-info" style="font-size:10px;">AI</span>';
        }
        const ref = row.linked_crm_lead || row.linked_reference_doctype || '';
        if (!ref) return '';
        const label = row.linked_crm_lead ? 'CRM' : row.linked_reference_doctype;
        return `<span class="badge badge-light" style="font-size:10px;">${wa_chat_hub.notifications.escape(label)}</span>`;
    },

    escape: function(value) {
        return $('<div>').text(value == null ? '' : String(value)).html();
    },

    clear_scope_and_open_all: function() {
        frappe.call({
            method: "wa_chat_hub.api.chat.clear_chat_hub_scope",
            type: "POST",
            always: function() {
                window.location.href = "/app/wa-chat-hub?scope=all";
            },
        });
    },

    bind_events: function() {
        frappe.realtime.on("wa_chat_new_message", function(data) {
            const message = (data || {}).message || {};
            const shouldRefresh = message.direction === "Inbound"
                || (message.direction === "Outbound" && message.sender_type === "AI");
            if (shouldRefresh) {
                // If dropdown is open, refresh it. Else, increment counter.
                if ($('.custom-wa-dropdown').hasClass('show')) {
                    wa_chat_hub.notifications.load_recent();
                }
                wa_chat_hub.notifications.refresh_count();
                if (message.direction === "Inbound") {
                    frappe.utils.play_sound("notification");
                }
            }
        });
    }
}
