frappe.pages['wa-chat-hub'].on_page_show = function(wrapper) {
    wrapper.wa_chat_hub_active = true;
    if (wrapper && typeof wrapper.wa_chat_hub_handle_route_options === 'function') {
        wrapper.wa_chat_hub_handle_route_options();
    }
};

frappe.pages['wa-chat-hub'].on_page_hide = function(wrapper) {
    wrapper.wa_chat_hub_active = false;
    if (wrapper.wa_chat_hub_refresh_state && wrapper.wa_chat_hub_refresh_state.timer) {
        clearTimeout(wrapper.wa_chat_hub_refresh_state.timer);
        wrapper.wa_chat_hub_refresh_state.timer = null;
    }
    if (wrapper.wa_chat_hub_refresh_state) {
        wrapper.wa_chat_hub_refresh_state.pending = false;
    }
};

frappe.pages['wa-chat-hub'].on_page_load = function(wrapper) {
    if (wrapper.wa_chat_hub_initialized) {
        return;
    }
    wrapper.wa_chat_hub_initialized = true;

    const cssVersion = '20260618-lead-temperature-filter-v1';
    const existingCss = document.querySelector('link[data-wa-chat-hub-css="1"]');
    if (existingCss && existingCss.getAttribute('data-wa-chat-hub-version') !== cssVersion) {
        existingCss.remove();
    }
    if (!document.querySelector('link[data-wa-chat-hub-css="1"]')) {
        $('<link>', {
            rel: 'stylesheet',
            href: `/assets/wa_chat_hub/css/wa_chat_hub.css?v=${cssVersion}`,
            'data-wa-chat-hub-css': '1',
            'data-wa-chat-hub-version': cssVersion
        }).appendTo('head');
    }

    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: 'WA Chat Hub',
        single_column: true
    });
    $(wrapper).addClass('wa-chat-hub-page');

    let currentConversation = null;
    let conversationLoadInFlightName = null;
    let conversationLoadToken = 0;
    let sidebarContextCache = null;
    let messagingWindowCache = null;
    const conversationRefreshState = wrapper.wa_chat_hub_refresh_state || {
        timer: null,
        inFlight: false,
        pending: false,
        lastAt: 0,
    };
    wrapper.wa_chat_hub_refresh_state = conversationRefreshState;
    const CONVERSATION_REFRESH_DEBOUNCE_MS = 3000;
    let conversationSearchTimer = null;
    let preselectedConversation = null;
    let selectedChannelAccount = '';
    let channelAccounts = [];
    let conversationRowsCache = [];
    let conversationSearchQuery = '';
    let activeConversationFilter = 'all';
    let selectedReferenceDoctype = '';
    let referenceFilterLocked = false;

    function isWaChatHubCurrentRoute() {
        const route = frappe.get_route ? frappe.get_route() : [];
        return route && route[0] === 'wa-chat-hub' && wrapper.wa_chat_hub_active !== false;
    }

    function isWaChatHubRouteActive() {
        return isWaChatHubCurrentRoute() && !document.hidden;
    }

    page.add_inner_button(__('List View'), () => {
        frappe.set_route('List', 'Chat Conversation');
    });

    page.main.html(`
        <div class="wa-chat-hub-layout is-thread-empty" id="wa-chat-hub-layout">
            <aside class="wa-left-pane">
                <div class="wa-pane-heading wa-left-header">
                    <div class="wa-pane-title">WA Chat Hub</div>
                    <div class="wa-left-tools">
                        <button class="wa-left-tool wa-autopilot-toggle is-on hidden" id="wa-autopilot-toggle" title="AI auto-reply: On" type="button" aria-pressed="true">
                            <i class="fa fa-magic"></i>
                        </button>
                        <button class="wa-left-tool" id="wa-list-view-btn" title="List View" type="button"><i class="fa fa-plus-square-o"></i></button>
                        <button class="wa-left-tool" title="More" type="button"><i class="fa fa-ellipsis-v"></i></button>
                    </div>
                </div>
                <div class="wa-search-wrap">
                    <i class="fa fa-search"></i>
                    <input class="wa-search" id="wa-search" placeholder="Search or start a new chat" />
                </div>
                <div class="wa-account-filter-wrap">
                    <label class="wa-account-filter-label" for="wa-channel-account-filter">Account</label>
                    <select class="form-control input-sm" id="wa-channel-account-filter" title="Filter by Interakt account">
                        <option value="">All accounts</option>
                    </select>
                </div>
                <div class="wa-reference-filter-bar hidden" id="wa-reference-filter-bar">
                    <span id="wa-reference-filter-label"></span>
                    <button type="button" id="wa-reference-filter-clear" title="Clear filter"><i class="fa fa-times"></i></button>
                </div>
                <div class="wa-filter-row">
                    <button class="wa-chip active" data-filter="all">All</button>
                    <button class="wa-chip" data-filter="unread">Unread</button>
                    <button class="wa-chip" data-filter="unassigned">Unassigned</button>
                    <div class="wa-filter-more-wrap">
                        <button class="wa-chip wa-chip-icon" id="wa-filter-more-btn" title="Lead temperature filters" type="button"><i class="fa fa-plus"></i></button>
                        <div class="wa-filter-menu" id="wa-filter-menu">
                            <button type="button" class="wa-filter-menu-item" data-filter="temperature_hot">Hot</button>
                            <button type="button" class="wa-filter-menu-item" data-filter="temperature_warm">Warm</button>
                            <button type="button" class="wa-filter-menu-item" data-filter="temperature_cold">Cold</button>
                        </div>
                    </div>
                </div>
                <div class="wa-conversation-list" id="wa-conversation-list"><div class="wa-empty">Loading...</div></div>
            </aside>
            <main class="wa-center-pane is-empty" id="wa-center-pane">
                <div class="wa-thread-header">
                    <button class="wa-thread-profile" id="wa-thread-profile" type="button" title="Open contact info">
                        <span class="wa-thread-avatar" id="wa-thread-avatar">?</span>
                        <span class="wa-thread-identity">
                            <span class="wa-pane-title" id="wa-thread-title">Thread</span>
                            <span class="wa-thread-subtitle" id="wa-thread-subtitle">Select a conversation to inspect message history.</span>
                        </span>
                    </button>
                    <div class="wa-thread-actions">
                        <button class="btn btn-sm wa-autopilot-pill is-on hidden" id="wa-autopilot-pill" type="button" title="AI auto-reply: On">
                            <i class="fa fa-magic"></i> <span id="wa-autopilot-pill-label">AI On</span>
                        </button>
                        <button class="btn btn-default btn-sm" id="wa-ai-summary">AI Summary</button>
                        <button class="btn btn-default btn-sm" id="wa-ai-draft">AI Draft</button>
                    </div>
                </div>
                <div class="wa-message-list" id="wa-message-list">
                    <div class="wa-thread-empty-state">
                        <div class="wa-thread-empty-icon"><i class="fa fa-whatsapp"></i></div>
                        <div class="wa-thread-empty-title">WA Chat Hub</div>
                        <div class="wa-thread-empty-text">Select a conversation to view messages and reply.</div>
                    </div>
                </div>
                <div class="wa-messaging-window-banner is-hidden" id="wa-messaging-window-banner" role="status"></div>
                <div class="wa-composer" id="wa-composer">
                    <div class="wa-attach-wrap">
                        <button class="wa-composer-icon" id="wa-attach-btn" title="Attach" type="button">${appIcon('attach')}</button>
                        <div class="wa-attach-menu" id="wa-attach-menu">
                            <button type="button" class="wa-attach-item" data-action="document"><span class="wa-attach-icon document"><i class="fa fa-file-text"></i></span>Document</button>
                            <button type="button" class="wa-attach-item" data-action="photos"><span class="wa-attach-icon photos"><i class="fa fa-image"></i></span>Photos &amp; Videos</button>
                            <button type="button" class="wa-attach-item" data-action="camera"><span class="wa-attach-icon camera"><i class="fa fa-camera"></i></span>Camera</button>
                            <button type="button" class="wa-attach-item" data-action="audio"><span class="wa-attach-icon audio"><i class="fa fa-headphones"></i></span>Audio</button>
                            <button type="button" class="wa-attach-item" data-action="contact"><span class="wa-attach-icon contact"><i class="fa fa-user"></i></span>Contact</button>
                            <button type="button" class="wa-attach-item" data-action="poll"><span class="wa-attach-icon poll"><i class="fa fa-tasks"></i></span>Poll</button>
                            <button type="button" class="wa-attach-item" data-action="event"><span class="wa-attach-icon event"><i class="fa fa-calendar"></i></span>Event</button>
                            <button type="button" class="wa-attach-item" data-action="sticker"><span class="wa-attach-icon sticker"><i class="fa fa-plus-circle"></i></span>New sticker</button>
                        </div>
                    </div>
                    <button class="wa-composer-icon" id="wa-template-btn" title="Template" type="button">${appIcon('emoji')}</button>
                    <textarea class="form-control" rows="1" id="wa-composer-body" placeholder="Type a message"></textarea>
                    <button class="wa-send-circle" id="wa-send-btn" title="Send" type="button"><i class="fa fa-paper-plane"></i></button>
                </div>
            </main>
            <aside class="wa-right-pane is-empty" id="wa-right-pane">
                <div class="wa-right-header">
                    <button class="wa-right-close" id="wa-right-close" title="Close contact info" type="button">
                        <svg viewBox="0 0 24 24" height="24" width="24" preserveAspectRatio="xMidYMid meet" fill="currentColor" aria-hidden="true" focusable="false">
                            <path d="M12 13.4L7.09999 18.3C6.91665 18.4834 6.68332 18.575 6.39999 18.575C6.11665 18.575 5.88332 18.4834 5.69999 18.3C5.51665 18.1167 5.42499 17.8834 5.42499 17.6C5.42499 17.3167 5.51665 17.0834 5.69999 16.9L10.6 12L5.69999 7.10005C5.51665 6.91672 5.42499 6.68338 5.42499 6.40005C5.42499 6.11672 5.51665 5.88338 5.69999 5.70005C5.88332 5.51672 6.11665 5.42505 6.39999 5.42505C6.68332 5.42505 6.91665 5.51672 7.09999 5.70005L12 10.6L16.9 5.70005C17.0833 5.51672 17.3167 5.42505 17.6 5.42505C17.8833 5.42505 18.1167 5.51672 18.3 5.70005C18.4833 5.88338 18.575 6.11672 18.575 6.40005C18.575 6.68338 18.4833 6.91672 18.3 7.10005L13.4 12L18.3 16.9C18.4833 17.0834 18.575 17.3167 18.575 17.6C18.575 17.8834 18.4833 18.1167 18.3 18.3C18.1167 18.4834 17.8833 18.575 17.6 18.575C17.3167 18.575 17.0833 18.4834 16.9 18.3L12 13.4Z"></path>
                        </svg>
                    </button>
                    <div class="wa-pane-title">Contact info</div>
                </div>
                <div class="wa-card" id="wa-context-card"><div class="wa-empty">Select a conversation to view context.</div></div>
                <div class="wa-card wa-context-dependent">
                    <div class="wa-card-title">ERP Actions</div>
                    <div class="wa-action-list">
                        <button class="btn btn-default btn-sm" id="wa-create-lead">Create Lead</button>
                        <button class="btn btn-default btn-sm" id="wa-create-issue">Create Support Ticket</button>
                    </div>
                </div>
                <div class="wa-card wa-context-dependent">
                    <div class="wa-card-title">AI Output</div>
                    <pre class="wa-pre" id="wa-ai-output">No AI output yet.</pre>
                </div>
            </aside>
        </div>
        <div class="wa-media-viewer" id="wa-media-viewer" aria-hidden="true">
            <div class="wa-media-viewer-top">
                <div class="wa-media-viewer-meta">
                    <div class="wa-media-viewer-title" id="wa-media-viewer-title">Image</div>
                    <div class="wa-media-viewer-subtitle" id="wa-media-viewer-subtitle"></div>
                </div>
                <div class="wa-media-viewer-actions">
                    <button class="wa-media-viewer-btn" id="wa-media-viewer-zoom-out" title="Zoom out" type="button">${viewerIcon('zoom-out')}</button>
                    <button class="wa-media-viewer-btn" id="wa-media-viewer-zoom-in" title="Zoom in" type="button">${viewerIcon('zoom-in')}</button>
                    <button class="wa-media-viewer-btn" title="Details" type="button">${viewerIcon('details')}</button>
                    <button class="wa-media-viewer-btn" title="Reply" type="button">${viewerIcon('reply')}</button>
                    <button class="wa-media-viewer-btn" title="Star" type="button">${viewerIcon('star')}</button>
                    <button class="wa-media-viewer-btn" title="Pin" type="button">${viewerIcon('pin')}</button>
                    <button class="wa-media-viewer-btn" title="React" type="button">${viewerIcon('smile')}</button>
                    <button class="wa-media-viewer-btn" title="Forward" type="button">${viewerIcon('forward')}</button>
                    <button class="wa-media-viewer-btn" id="wa-media-viewer-open" title="Open in new tab" type="button">${viewerIcon('details')}</button>
                    <button class="wa-media-viewer-btn" id="wa-media-viewer-download" title="Download" type="button">${viewerIcon('download')}</button>
                    <button class="wa-media-viewer-btn" title="More" type="button">${viewerIcon('more')}</button>
                    <button class="wa-media-viewer-btn" id="wa-media-viewer-close" title="Close" type="button">${viewerIcon('close')}</button>
                </div>
            </div>
            <button class="wa-media-viewer-nav prev" id="wa-media-viewer-prev" title="Previous" type="button"><i class="fa fa-chevron-left"></i></button>
            <div class="wa-media-viewer-stage">
                <img id="wa-media-viewer-image" alt="Media preview" />
            </div>
            <button class="wa-media-viewer-nav next" id="wa-media-viewer-next" title="Next" type="button"><i class="fa fa-chevron-right"></i></button>
            <div class="wa-media-viewer-strip" id="wa-media-viewer-strip"></div>
        </div>
    `);

    const api = {
        channelAccounts: () => frappe.call('wa_chat_hub.api.chat.get_channel_accounts'),
        conversations: () => frappe.call('wa_chat_hub.api.chat.get_conversations', {
            limit: 50,
            channel_account: selectedChannelAccount || null,
            reference_doctype: selectedReferenceDoctype || null,
            lead_temperature: getActiveLeadTemperatureFilter() || null,
        }),
        searchConversations: (query) => frappe.call('wa_chat_hub.api.chat.search_conversations', {
            query,
            limit: 50,
            channel_account: selectedChannelAccount || null,
            reference_doctype: selectedReferenceDoctype || null,
            lead_temperature: getActiveLeadTemperatureFilter() || null,
        }),
        messages: (conversation) => frappe.call('wa_chat_hub.api.chat.get_messages', { conversation, limit: 200 }),
        context: (conversation) => frappe.call('wa_chat_hub.api.chat.get_sidebar_context', { conversation }),
        markRead: (conversation) => frappe.call('wa_chat_hub.api.chat.mark_read', { conversation }),
        aiSummary: (conversation) => frappe.call('wa_chat_hub.api.ai.summarize_conversation', { conversation }),
        aiDraft: (conversation) => frappe.call('wa_chat_hub.api.ai.draft_reply', { conversation }),
        sendReply: (conversation, body, content_type = 'Text', media_url = null) => frappe.call({
            method: 'wa_chat_hub.api.runtime.send_reply',
            type: 'POST',
            args: { conversation, body, content_type, media_url },
        }),
        sendMediaReply: (conversation, body, content_type, media_url, display_media_url = null, file_name = null, file_size = null, attachment_file = null) => frappe.call({
            method: 'wa_chat_hub.api.runtime.send_reply',
            type: 'POST',
            args: { conversation, body, content_type, media_url, display_media_url, file_name, file_size, attachment_file },
        }),
        getInteraktTemplates: (args) => frappe.call('wa_chat_hub.api.runtime.get_interakt_templates', args),
        sendTemplate: (args) => frappe.call({
            method: 'wa_chat_hub.api.runtime.send_template_message',
            type: 'POST',
            args: args,
        }),
        createLead: (conversation) => frappe.call('wa_chat_hub.api.actions.create_lead_from_conversation', { conversation }),
        createIssue: (conversation) => frappe.call('wa_chat_hub.api.actions.create_issue_from_conversation', { conversation }),
        getAutopilotStatus: () => frappe.call('wa_chat_hub.api.settings.get_autopilot_status'),
        setAutopilotEnabled: (enabled) => frappe.call('wa_chat_hub.api.settings.set_autopilot_enabled', { enabled: enabled ? 1 : 0 }),
        getChatHubScope: () => frappe.call('wa_chat_hub.api.chat.get_chat_hub_scope'),
        clearChatHubScope: () => frappe.call({
            method: 'wa_chat_hub.api.chat.clear_chat_hub_scope',
            type: 'POST',
        }),
    };

    function updateAutopilotToggleUI(enabled, sendsWhatsapp) {
        const on = !!enabled;
        const sends = !!sendsWhatsapp;
        const title = on
            ? (sends ? 'AI auto-reply: On (sends on WhatsApp)' : 'AI on (draft/suggest only — not sending)')
            : 'AI auto-reply: Off — click to enable';
        const label = on ? (sends ? 'AI On' : 'AI Draft') : 'AI Off';

        $('#wa-autopilot-toggle')
            .toggleClass('is-on', on)
            .toggleClass('is-off', !on)
            .attr('title', title)
            .attr('aria-pressed', on ? 'true' : 'false');
        $('#wa-autopilot-pill')
            .toggleClass('is-on', on)
            .toggleClass('is-off', !on)
            .toggleClass('sends-whatsapp', on && sends)
            .attr('title', title);
        $('#wa-autopilot-pill-label').text(label);
    }

    function loadAutopilotStatus() {
        return api.getAutopilotStatus().then((r) => {
            const data = (r.message || {});
            updateAutopilotToggleUI(data.enabled, data.sends_whatsapp);
            return data;
        });
    }

    function toggleAutopilot() {
        const $btn = $('#wa-autopilot-toggle');
        if ($btn.prop('disabled')) return;
        const turnOn = !$btn.hasClass('is-on');
        $btn.prop('disabled', true);
        $('#wa-autopilot-pill').prop('disabled', true);

        api.setAutopilotEnabled(turnOn).then((r) => {
            const data = (r.message || {});
            updateAutopilotToggleUI(data.enabled, data.sends_whatsapp);
            frappe.show_alert({
                message: data.enabled
                    ? (data.sends_whatsapp
                        ? __('AI auto-reply enabled — replies will be sent on WhatsApp')
                        : __('AI is on but not sending. Open WA Chat Hub Settings and set Autopilot Mode to Limited Auto Reply.'))
                    : __('AI auto-reply stopped'),
                indicator: data.enabled ? (data.sends_whatsapp ? 'green' : 'orange') : 'orange',
            });
        }).catch((err) => {
            frappe.show_alert({ message: err.message || __('Could not update AI setting'), indicator: 'red' });
            return loadAutopilotStatus();
        }).always(() => {
            $btn.prop('disabled', false);
            $('#wa-autopilot-pill').prop('disabled', false);
        });
    }

    $('#wa-autopilot-toggle, #wa-autopilot-pill').on('click', function() {
        toggleAutopilot();
    });

    loadAutopilotStatus();

    $('#wa-list-view-btn').on('click', function() {
        frappe.set_route('List', 'Chat Conversation');
    });

    $('#wa-thread-profile').on('click', function() {
        if (!currentConversation) return;
        openContextDrawer();
    });

    $('#wa-right-close').on('click', function() {
        closeContextDrawer();
    });

    $('#wa-context-card').on('click', '#wa-source-toggle', function() {
        const $card = $('#wa-source-card');
        const isOpen = $card.toggleClass('show').hasClass('show');
        $(this).toggleClass('open', isOpen);
        $(this).find('span').text(isOpen ? 'Show Less' : 'Show More');
    });

    function renderChannelAccountFilter() {
        const $select = $('#wa-channel-account-filter');
        const options = ['<option value="">All accounts</option>'];
        channelAccounts.forEach((row) => {
            const label = row.account_name || row.name;
            const suffix = row.channel_type ? ` (${row.channel_type})` : '';
            options.push(`<option value="${escapeHtml(row.name)}">${escapeHtml(label + suffix)}</option>`);
        });
        $select.html(options.join(''));
        $select.val(selectedChannelAccount || '');
    }

    function loadChannelAccounts() {
        return api.channelAccounts().then((r) => {
            channelAccounts = (r.message || {}).result || [];
            renderChannelAccountFilter();
        });
    }

    function renderConversations(rows) {
        const html = rows.length ? rows.map(row => `
            <button class="wa-conversation-item ${row.name === currentConversation ? 'active' : ''}" data-name="${escapeHtml(row.name)}">
                <div class="wa-conversation-avatar">${getAvatarText(row)}</div>
                <div class="wa-conversation-main">
                    <div class="wa-conversation-top">
                        <strong>${escapeHtml(row.contact_display_name || row.contact_phone_number || row.name)}</strong>
                        <span class="wa-conversation-time">${formatConversationTime(row.last_message_time || row.modified)}</span>
                    </div>
                    ${row.channel_account ? `<div class="wa-conversation-account">${escapeHtml(row.channel_account)}</div>` : ''}
                    <div class="wa-conversation-meta">
                        ${renderLeadMeta(row)}
                    </div>
                    <div class="wa-conversation-bottom">
                        ${renderConversationPreview(row.last_message_preview || '')}
                        ${(row.unread_count || 0) ? `<span class="wa-badge">${row.unread_count}</span>` : ''}
                    </div>
                </div>
            </button>
        `).join('') : '<div class="wa-empty">No conversations found.</div>';
        $('#wa-conversation-list').html(html);
        $('.wa-conversation-item').on('click', function() {
            loadConversation($(this).data('name'));
        });
    }

    function renderLeadMeta(row) {
        const score = Number(row.lead_score || 0).toFixed(1);
        const lan = escapeHtml(row.lead_lan || 'Unknown');
        const temp = escapeHtml(row.lead_temperature || 'Cold');
        return `
            <span class="wa-meta-chip">lead_score: ${score}</span>
            <span class="wa-meta-chip">lead_lan: ${lan}</span>
            <span class="wa-meta-chip">Lead_temperature: ${temp}</span>
        `;
    }

    function renderConversationPreview(preview) {
        const parsed = parseConversationPreview(preview);
        if (parsed.type === 'Document') {
            return `
                <span class="wa-conversation-preview has-icon">
                    <i class="fa fa-file-text-o"></i>
                    <span>${escapeHtml(parsed.text || 'Document')}</span>
                </span>
            `;
        }
        if (parsed.type === 'Image') {
            const imageText = isGenericMediaPreview(parsed.text) ? 'Photo' : parsed.text;
            return `
                <span class="wa-conversation-preview has-icon">
                    <i class="fa fa-image"></i>
                    <span>${escapeHtml(imageText || 'Photo')}</span>
                </span>
            `;
        }
        return `<span class="wa-conversation-preview">${escapeHtml(parsed.text)}</span>`;
    }

    function parseConversationPreview(preview) {
        const text = String(preview || '').trim();
        if (text.toLowerCase() === '[image message received]') {
            return {type: 'Image', text: 'Photo'};
        }
        const match = text.match(/^\[([^\]]+)\]\s*(.*)$/);
        if (!match) return {type: 'Text', text};
        return {type: match[1], text: match[2] || match[1]};
    }

    function isGenericMediaPreview(text) {
        const normalized = String(text || '').trim().toLowerCase();
        return !normalized || normalized === 'image' || normalized === 'image message received' || normalized === 'none';
    }

    function getAvatarText(row) {
        const value = row.contact_display_name || row.contact_phone_number || row.name || '';
        return frappe.utils.escape_html(String(value).trim().charAt(0).toUpperCase() || '?');
    }

    function formatConversationTime(value) {
        if (!value) return '';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return '';
        const now = new Date();
        const sameDay = date.toDateString() === now.toDateString();
        if (sameDay) {
            return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
        }
        return date.toLocaleDateString([], {month: 'short', day: 'numeric'});
    }

    $('#wa-channel-account-filter').on('change', function() {
        selectedChannelAccount = $(this).val() || '';
        conversationSearchQuery = '';
        $('#wa-search').val('');
        closeCurrentConversation();
        refreshConversations();
    });

    function renderReferenceFilter() {
        const $bar = $('#wa-reference-filter-bar');
        if (!selectedReferenceDoctype) {
            $bar.addClass('hidden');
            $('#wa-reference-filter-label').text('');
            return;
        }
        const label = selectedReferenceDoctype === 'Patient'
            ? __('Patients')
            : selectedReferenceDoctype;
        $('#wa-reference-filter-label').text(__('Showing {0} chats', [label]));
        $('#wa-reference-filter-clear').toggleClass('hidden', referenceFilterLocked);
        $bar.removeClass('hidden');
    }

    $('#wa-reference-filter-clear').on('click', function() {
        if (referenceFilterLocked) {
            return;
        }
        selectedReferenceDoctype = '';
        referenceFilterLocked = false;
        renderReferenceFilter();
        persistWaRouteQuery('', '', false);
        closeCurrentConversation();
        refreshConversations();
    });

    function applyClientConversationFilters(rows) {
        let filtered = rows || [];
        if (activeConversationFilter === 'unread') {
            filtered = filtered.filter((row) => cint(row.unread_count) > 0);
        } else if (activeConversationFilter === 'unassigned') {
            filtered = filtered.filter((row) => !row.assigned_to);
        } else if (getActiveLeadTemperatureFilter()) {
            const selectedTemperature = getActiveLeadTemperatureFilter().toLowerCase();
            filtered = filtered.filter((row) => String(row.lead_temperature || '').trim().toLowerCase() === selectedTemperature);
        }
        return filtered;
    }

    function getActiveLeadTemperatureFilter() {
        const match = String(activeConversationFilter || '').match(/^temperature_(hot|warm|cold)$/);
        if (!match) return '';
        return match[1].charAt(0).toUpperCase() + match[1].slice(1);
    }

    function applyConversationListView() {
        if (conversationSearchQuery) {
            return api.searchConversations(conversationSearchQuery).then((r) => {
                const rows = applyClientConversationFilters((r.message || {}).result || []);
                renderConversations(rows);
            });
        }
        const rows = applyClientConversationFilters(conversationRowsCache);
        renderConversations(rows);
        return Promise.resolve();
    }

    function refreshConversations(force = false) {
        if (!isWaChatHubRouteActive()) {
            if (isWaChatHubCurrentRoute() && document.hidden) {
                conversationRefreshState.pending = true;
                return Promise.resolve();
            }
            conversationRefreshState.pending = false;
            clearTimeout(conversationRefreshState.timer);
            return Promise.resolve();
        }

        if (conversationRefreshState.inFlight) {
            conversationRefreshState.pending = true;
            return Promise.resolve();
        }

        const now = Date.now();
        if (!force && conversationRefreshState.lastAt && now - conversationRefreshState.lastAt < CONVERSATION_REFRESH_DEBOUNCE_MS) {
            conversationRefreshState.pending = true;
            scheduleConversationRefresh(CONVERSATION_REFRESH_DEBOUNCE_MS - (now - conversationRefreshState.lastAt));
            return Promise.resolve();
        }

        conversationRefreshState.inFlight = true;
        conversationRefreshState.pending = false;
        conversationRefreshState.lastAt = now;

        if (!isWaChatHubRouteActive()) {
            conversationRefreshState.inFlight = false;
            return Promise.resolve();
        }

        return Promise.resolve(api.conversations()).then((r) => {
            if (!isWaChatHubCurrentRoute()) {
                return Promise.resolve();
            }
            conversationRowsCache = (r.message || {}).result || [];
            return applyConversationListView();
        }).then(() => {
            if (!isWaChatHubCurrentRoute()) {
                return;
            }
            if (preselectedConversation) {
                const target = conversationRowsCache.find((row) => row.name === preselectedConversation);
                if (target) {
                    loadConversation(preselectedConversation);
                }
                preselectedConversation = null;
            }
        }).finally(() => {
            conversationRefreshState.inFlight = false;
            if (conversationRefreshState.pending && isWaChatHubCurrentRoute()) {
                conversationRefreshState.pending = false;
                scheduleConversationRefresh(CONVERSATION_REFRESH_DEBOUNCE_MS);
            }
        });
    }

    function consumeRouteConversation() {
        const queryOptions = getWaRouteQueryOptions();
        const routeOptions = {
            conversation: (frappe.route_options || {}).conversation || queryOptions.conversation,
            reference_doctype: (frappe.route_options || {}).reference_doctype || queryOptions.reference_doctype,
            lock_reference_filter: cint(
                (frappe.route_options || {}).lock_reference_filter || queryOptions.lock_reference_filter
            ),
        };
        const routeConversation = routeOptions.conversation;
        const routeReferenceDoctype = routeOptions.reference_doctype;
        const routeLockReferenceFilter = routeOptions.lock_reference_filter;
        if (queryOptions.scope === 'all') {
            selectedReferenceDoctype = '';
            referenceFilterLocked = false;
            renderReferenceFilter();
            return api.clearChatHubScope().then(() => {
                persistWaRouteQuery('', '', false);
                return false;
            });
        }
        if (!routeConversation && !routeReferenceDoctype) {
            return api.getChatHubScope().then((r) => {
                const scope = ((r.message || {}).scope) || {};
                if (!scope.reference_doctype || !scope.locked) {
                    return false;
                }
                applyRouteScope({
                    reference_doctype: scope.reference_doctype,
                    lock_reference_filter: 1,
                });
                refreshConversations();
                return true;
            });
        }

        if (frappe.route_options) {
            delete frappe.route_options.conversation;
            delete frappe.route_options.reference_doctype;
            delete frappe.route_options.lock_reference_filter;
        }
        applyRouteScope(routeOptions);
        if (routeConversation && currentConversation !== routeConversation) {
            loadConversation(routeConversation);
        }
        refreshConversations();
        return true;
    }

    function applyRouteScope(options) {
        if (!options.reference_doctype) {
            return;
        }
        selectedReferenceDoctype = options.reference_doctype;
        referenceFilterLocked = !!cint(options.lock_reference_filter);
        renderReferenceFilter();
        persistWaRouteQuery(options.conversation, options.reference_doctype, referenceFilterLocked);
        conversationSearchQuery = '';
        $('#wa-search').val('');
    }

    function getWaRouteQueryOptions() {
        const params = new URLSearchParams(window.location.search || '');
        return {
            conversation: params.get('conversation') || '',
            reference_doctype: params.get('reference_doctype') || '',
            lock_reference_filter: params.get('lock_reference_filter') || '',
            scope: params.get('scope') || '',
        };
    }

    function persistWaRouteQuery(conversation, referenceDoctype, lockReferenceFilter) {
        const params = new URLSearchParams();
        if (referenceDoctype) {
            params.set('reference_doctype', referenceDoctype);
        }
        if (lockReferenceFilter) {
            params.set('lock_reference_filter', '1');
        }
        if (conversation) {
            params.set('conversation', conversation);
        }
        const nextUrl = params.toString() ? `/app/wa-chat-hub?${params.toString()}` : '/app/wa-chat-hub';
        if (window.location.pathname === '/app/wa-chat-hub' && window.location.search !== `?${params.toString()}`) {
            window.history.replaceState(window.history.state, '', nextUrl);
        }
    }

    wrapper.wa_chat_hub_handle_route_options = consumeRouteConversation;

    function debounceConversationSearch(fn, waitMs) {
        return function (...args) {
            clearTimeout(conversationSearchTimer);
            conversationSearchTimer = setTimeout(() => fn.apply(this, args), waitMs);
        };
    }

    $('#wa-search').on('input', debounceConversationSearch(function () {
        conversationSearchQuery = String($('#wa-search').val() || '').trim();
        applyConversationListView();
    }, 280));

    $('#wa-search').on('keydown', function (e) {
        if (e.key === 'Escape') {
            conversationSearchQuery = '';
            $(this).val('');
            applyConversationListView();
        }
    });

    function setConversationFilter(filter) {
        const previousTemperature = getActiveLeadTemperatureFilter();
        activeConversationFilter = filter || 'all';
        $('.wa-filter-row .wa-chip').removeClass('active');
        $('#wa-filter-menu .wa-filter-menu-item').removeClass('active');
        const temperature = getActiveLeadTemperatureFilter();
        if (temperature) {
            $('#wa-filter-more-btn')
                .addClass('active')
                .attr('title', __('Lead temperature: {0}', [temperature]));
            $(`#wa-filter-menu .wa-filter-menu-item[data-filter="${activeConversationFilter}"]`).addClass('active');
        } else {
            $(`.wa-filter-row .wa-chip[data-filter="${activeConversationFilter}"]`).addClass('active');
            $('#wa-filter-more-btn').attr('title', __('Lead temperature filters'));
        }
        $('#wa-filter-menu').removeClass('show');
        if (previousTemperature !== temperature) {
            refreshConversations(true);
            return;
        }
        applyConversationListView();
    }

    $('.wa-filter-row').on('click', '.wa-chip:not(.wa-chip-icon)', function () {
        setConversationFilter($(this).data('filter') || 'all');
    });

    $('#wa-filter-more-btn').on('click', function(e) {
        e.stopPropagation();
        $('#wa-filter-menu').toggleClass('show');
    });

    $('#wa-filter-menu').on('click', '.wa-filter-menu-item', function(e) {
        e.stopPropagation();
        setConversationFilter($(this).data('filter') || 'all');
    });

    $(document).on('click.waFilterMenu', function(e) {
        if (!$(e.target).closest('.wa-filter-more-wrap').length) {
            $('#wa-filter-menu').removeClass('show');
        }
    });

    function escapeHtml(value) {
        return frappe.utils.escape_html(value || '');
    }

    function viewerIcon(name) {
        const icons = {
            'zoom-out': '<path fill="currentColor" d="M8 10.5C7.71667 10.5 7.47917 10.4042 7.2875 10.2125C7.09583 10.0208 7 9.78333 7 9.5C7 9.21667 7.09583 8.97917 7.2875 8.7875C7.47917 8.59583 7.71667 8.5 8 8.5H11C11.2833 8.5 11.5208 8.59583 11.7125 8.7875C11.9042 8.97917 12 9.21667 12 9.5C12 9.78333 11.9042 10.0208 11.7125 10.2125C11.5208 10.4042 11.2833 10.5 11 10.5H8ZM9.5 16C7.68333 16 6.14583 15.3708 4.8875 14.1125C3.62917 12.8542 3 11.3167 3 9.5C3 7.68333 3.62917 6.14583 4.8875 4.8875C6.14583 3.62917 7.68333 3 9.5 3C11.3167 3 12.8542 3.62917 14.1125 4.8875C15.3708 6.14583 16 7.68333 16 9.5C16 10.2333 15.8833 10.925 15.65 11.575C15.4167 12.225 15.1 12.8 14.7 13.3L20.3 18.9C20.4833 19.0833 20.575 19.3167 20.575 19.6C20.575 19.8833 20.4833 20.1167 20.3 20.3C20.1167 20.4833 19.8833 20.575 19.6 20.575C19.3167 20.575 19.0833 20.4833 18.9 20.3L13.3 14.7C12.8 15.1 12.225 15.4167 11.575 15.65C10.925 15.8833 10.2333 16 9.5 16ZM9.5 14C10.75 14 11.8125 13.5625 12.6875 12.6875C13.5625 11.8125 14 10.75 14 9.5C14 8.25 13.5625 7.1875 12.6875 6.3125C11.8125 5.4375 10.75 5 9.5 5C8.25 5 7.1875 5.4375 6.3125 6.3125C5.4375 7.1875 5 8.25 5 9.5C5 10.75 5.4375 11.8125 6.3125 12.6875C7.1875 13.5625 8.25 14 9.5 14Z"></path>',
            'zoom-in': '<path fill="currentColor" d="M8.5 10.5H7.5C7.21667 10.5 6.97917 10.4042 6.7875 10.2125C6.59583 10.0208 6.5 9.78333 6.5 9.5C6.5 9.21667 6.59583 8.97917 6.7875 8.7875C6.97917 8.59583 7.21667 8.5 7.5 8.5H8.5V7.5C8.5 7.21667 8.59583 6.97917 8.7875 6.7875C8.97917 6.59583 9.21667 6.5 9.5 6.5C9.78333 6.5 10.0208 6.59583 10.2125 6.7875C10.4042 6.97917 10.5 7.21667 10.5 7.5V8.5H11.5C11.7833 8.5 12.0208 8.59583 12.2125 8.7875C12.4042 8.97917 12.5 9.21667 12.5 9.5C12.5 9.78333 12.4042 10.0208 12.2125 10.2125C12.0208 10.4042 11.7833 10.5 11.5 10.5H10.5V11.5C10.5 11.7833 10.4042 12.0208 10.2125 12.2125C10.0208 12.4042 9.78333 12.5 9.5 12.5C9.21667 12.5 8.97917 12.4042 8.7875 12.2125C8.59583 12.0208 8.5 11.7833 8.5 11.5V10.5ZM9.5 16C7.68333 16 6.14583 15.3708 4.8875 14.1125C3.62917 12.8542 3 11.3167 3 9.5C3 7.68333 3.62917 6.14583 4.8875 4.8875C6.14583 3.62917 7.68333 3 9.5 3C11.3167 3 12.8542 3.62917 14.1125 4.8875C15.3708 6.14583 16 7.68333 16 9.5C16 10.2333 15.8833 10.925 15.65 11.575C15.4167 12.225 15.1 12.8 14.7 13.3L20.3 18.9C20.4833 19.0833 20.575 19.3167 20.575 19.6C20.575 19.8833 20.4833 20.1167 20.3 20.3C20.1167 20.4833 19.8833 20.575 19.6 20.575C19.3167 20.575 19.0833 20.4833 18.9 20.3L13.3 14.7C12.8 15.1 12.225 15.4167 11.575 15.65C10.925 15.8833 10.2333 16 9.5 16ZM9.5 14C10.75 14 11.8125 13.5625 12.6875 12.6875C13.5625 11.8125 14 10.75 14 9.5C14 8.25 13.5625 7.1875 12.6875 6.3125C11.8125 5.4375 10.75 5 9.5 5C8.25 5 7.1875 5.4375 6.3125 6.3125C5.4375 7.1875 5 8.25 5 9.5C5 10.75 5.4375 11.8125 6.3125 12.6875C7.1875 13.5625 8.25 14 9.5 14Z"></path>',
            details: '<path fill="currentColor" d="M5 5.5h11.3c.3 0 .55.1.75.3l2.15 2.15c.2.2.3.45.3.75V17.5c0 .55-.2 1.02-.59 1.41-.39.4-.86.59-1.41.59H5c-.55 0-1.02-.2-1.41-.59A1.93 1.93 0 0 1 3 17.5v-10c0-.55.2-1.02.59-1.41.39-.4.86-.59 1.41-.59Zm0 2v10h12.5V9.1l-1.6-1.6H5Zm3 4h6.5c.28 0 .52.1.71.29.2.19.29.43.29.71s-.1.52-.29.71c-.19.2-.43.29-.71.29H8c-.28 0-.52-.1-.71-.29A.97.97 0 0 1 7 12.5c0-.28.1-.52.29-.71.19-.2.43-.29.71-.29Zm0 3h8c.28 0 .52.1.71.29.2.19.29.43.29.71s-.1.52-.29.71c-.19.2-.43.29-.71.29H8c-.28 0-.52-.1-.71-.29A.97.97 0 0 1 7 15.5c0-.28.1-.52.29-.71.19-.2.43-.29.71-.29Z"></path>',
            reply: '<path fill="currentColor" d="M6.82502 12L9.72502 14.9C9.92502 15.1 10.0209 15.3333 10.0125 15.6C10.0042 15.8667 9.90002 16.1 9.70002 16.3C9.50002 16.4833 9.26669 16.5792 9.00002 16.5875C8.73336 16.5958 8.50002 16.5 8.30002 16.3L3.70002 11.7C3.50002 11.5 3.40002 11.2667 3.40002 11C3.40002 10.7333 3.50002 10.5 3.70002 10.3L8.30002 5.69999C8.48336 5.51665 8.71252 5.42499 8.98752 5.42499C9.26252 5.42499 9.50002 5.51665 9.70002 5.69999C9.90002 5.89999 10 6.13749 10 6.41249C10 6.68749 9.90002 6.92499 9.70002 7.12499L6.82502 9.99999H16C17.3834 9.99999 18.5625 10.4875 19.5375 11.4625C20.5125 12.4375 21 13.6167 21 15V18C21 18.2833 20.9042 18.5208 20.7125 18.7125C20.5209 18.9042 20.2834 19 20 19C19.7167 19 19.4792 18.9042 19.2875 18.7125C19.0959 18.5208 19 18.2833 19 18V15C19 14.1667 18.7084 13.4583 18.125 12.875C17.5417 12.2917 16.8334 12 16 12H6.82502Z"></path>',
            star: '<path fill="currentColor" d="M8.85001 16.825L12 14.925L15.15 16.85L14.325 13.25L17.1 10.85L13.45 10.525L12 7.12495L10.55 10.5L6.90002 10.825L9.67501 13.25L8.85001 16.825ZM12 17.275L7.85001 19.775C7.66668 19.8916 7.47502 19.9416 7.27502 19.9249C7.07502 19.9083 6.90001 19.8416 6.75001 19.725C6.60001 19.6083 6.48335 19.4625 6.40002 19.2874C6.31668 19.1124 6.30001 18.9166 6.35001 18.7L7.45001 13.975L3.77502 10.8C3.60835 10.65 3.50418 10.4791 3.46252 10.2875C3.42085 10.0958 3.43335 9.90828 3.50001 9.72495C3.56668 9.54162 3.66668 9.39162 3.80001 9.27495C3.93335 9.15828 4.11668 9.08328 4.35001 9.04995L9.20001 8.62495L11.075 4.17495C11.1583 3.97495 11.2875 3.82495 11.4625 3.72495C11.6375 3.62495 11.8167 3.57495 12 3.57495C12.1833 3.57495 12.3625 3.62495 12.5375 3.72495C12.7125 3.82495 12.8417 3.97495 12.925 4.17495L14.8 8.62495L19.65 9.04995C19.8834 9.08328 20.0667 9.15828 20.2 9.27495C20.3333 9.39162 20.4333 9.54162 20.5 9.72495C20.5667 9.90828 20.5792 10.0958 20.5375 10.2875C20.4958 10.4791 20.3917 10.65 20.225 10.8L16.55 13.975L17.65 18.7C17.7 18.9166 17.6833 19.1124 17.6 19.2874C17.5167 19.4625 17.4 19.6083 17.25 19.725C17.1 19.8416 16.925 19.9083 16.725 19.9249C16.525 19.9416 16.3334 19.8916 16.15 19.775L12 17.275Z"></path>',
            pin: '<path fill="currentColor" d="M8 4h8c.28 0 .52.1.71.29.2.19.29.43.29.71s-.1.52-.29.71c-.19.2-.43.29-.71.29h-.5v4.1l2.2 2.9c.15.2.22.42.2.67-.02.25-.12.46-.3.63-.18.17-.4.25-.65.25H13v5.45c0 .28-.1.52-.29.71-.19.2-.43.29-.71.29s-.52-.1-.71-.29A.97.97 0 0 1 11 20v-5.45H7.05c-.25 0-.47-.08-.65-.25a.94.94 0 0 1-.3-.63c-.02-.25.05-.47.2-.67l2.2-2.9V6H8c-.28 0-.52-.1-.71-.29A.97.97 0 0 1 7 5c0-.28.1-.52.29-.71C7.48 4.1 7.72 4 8 4Zm2.5 2v4.45c0 .22-.07.43-.2.6L9.15 12.55h5.7l-1.15-1.5a1 1 0 0 1-.2-.6V6h-3Z"></path>',
            smile: '<path fill="currentColor" d="M12 21c-1.25 0-2.42-.24-3.5-.72a9.1 9.1 0 0 1-2.86-1.92A9.1 9.1 0 0 1 3.72 15.5 8.55 8.55 0 0 1 3 12c0-1.25.24-2.42.72-3.5a9.1 9.1 0 0 1 1.92-2.86A9.1 9.1 0 0 1 8.5 3.72 8.55 8.55 0 0 1 12 3c1.25 0 2.42.24 3.5.72a9.1 9.1 0 0 1 2.86 1.92 9.1 9.1 0 0 1 1.92 2.86c.48 1.08.72 2.25.72 3.5s-.24 2.42-.72 3.5a9.1 9.1 0 0 1-1.92 2.86 9.1 9.1 0 0 1-2.86 1.92A8.55 8.55 0 0 1 12 21Zm0-2c1.95 0 3.6-.68 4.95-2.05C18.32 15.6 19 13.95 19 12s-.68-3.6-2.05-4.95C15.6 5.68 13.95 5 12 5s-3.6.68-4.95 2.05C5.68 8.4 5 10.05 5 12s.68 3.6 2.05 4.95C8.4 18.32 10.05 19 12 19Zm-3.5-7.5c-.42 0-.77-.15-1.06-.44A1.45 1.45 0 0 1 7 10c0-.42.15-.77.44-1.06.29-.29.64-.44 1.06-.44s.77.15 1.06.44c.29.29.44.64.44 1.06s-.15.77-.44 1.06c-.29.29-.64.44-1.06.44Zm7 0c-.42 0-.77-.15-1.06-.44A1.45 1.45 0 0 1 14 10c0-.42.15-.77.44-1.06.29-.29.64-.44 1.06-.44s.77.15 1.06.44c.29.29.44.64.44 1.06s-.15.77-.44 1.06c-.29.29-.64.44-1.06.44ZM12 16.5c-1 0-1.91-.25-2.72-.76a5.17 5.17 0 0 1-1.88-2.04.72.72 0 0 1 .05-.74.73.73 0 0 1 .65-.36h7.8c.28 0 .5.12.65.36.15.24.17.49.05.74a5.17 5.17 0 0 1-1.88 2.04c-.81.51-1.72.76-2.72.76Z"></path>',
            forward: '<path fill="currentColor" d="M19.175 11L15.3 7.12499C15.1 6.92499 15 6.68749 15 6.41249C15 6.13749 15.1 5.89999 15.3 5.69999C15.5 5.51665 15.7375 5.42499 16.0125 5.42499C16.2875 5.42499 16.5167 5.51665 16.7 5.69999L21.3 10.3C21.4 10.4 21.4708 10.5083 21.5125 10.625C21.5542 10.7417 21.575 10.8667 21.575 11C21.575 11.1333 21.5542 11.2583 21.5125 11.375C21.4708 11.4917 21.4 11.6 21.3 11.7L16.7 16.3C16.5 16.5 16.2667 16.5958 16 16.5875C15.7333 16.5792 15.5 16.4833 15.3 16.3C15.1 16.1 14.9958 15.8667 14.9875 15.6C14.9792 15.3333 15.075 15.1 15.275 14.9L19.175 11ZM13.175 12H7C6.16667 12 5.45833 12.2917 4.875 12.875C4.29167 13.4583 4 14.1667 4 15V18C4 18.2833 3.90417 18.5208 3.7125 18.7125C3.52083 18.9042 3.28333 19 3 19C2.71667 19 2.47917 18.9042 2.2875 18.7125C2.09583 18.5208 2 18.28333 2 18V15C2 13.6167 2.4875 12.4375 3.4625 11.4625C4.4375 10.4875 5.61667 9.99999 7 9.99999H13.175L10.3 7.12499C10.1 6.92499 10 6.68749 10 6.41249C10 6.13749 10.1 5.89999 10.3 5.69999C10.5 5.51665 10.7375 5.42499 11.0125 5.42499C11.2875 5.42499 11.5167 5.51665 11.7 5.69999L16.3 10.3C16.4 10.4 16.4708 10.5083 16.5125 10.625C16.5542 10.7417 16.575 10.8667 16.575 11C16.575 11.1333 16.5542 11.2583 16.5125 11.375C16.4708 11.4917 16.4 11.6 16.3 11.7L11.7 16.3C11.5 16.5 11.2667 16.5958 11 16.5875C10.7333 16.5792 10.5 16.4833 10.3 16.3C10.1 16.1 9.99583 15.8667 9.9875 15.6C9.97917 15.3333 10.075 15.1 10.275 14.9L13.175 12Z"></path>',
            download: '<path fill="currentColor" d="M12 15.57a1.1 1.1 0 0 1-.38-.06.88.88 0 0 1-.32-.21l-3.6-3.6a.92.92 0 0 1-.29-.7c.01-.27.1-.5.29-.7.2-.2.44-.3.71-.31.28-.01.52.08.71.29L11 12.14V5c0-.28.1-.52.29-.71.19-.2.43-.29.71-.29.28 0 .52.1.71.29.2.19.29.43.29.71v7.15l1.88-1.88c.2-.2.43-.3.7-.28a1.02 1.02 0 0 1 1 1.01c.02.27-.08.5-.28.7l-3.6 3.6c-.1.1-.2.17-.32.21a1.1 1.1 0 0 1-.38.06ZM6 20c-.55 0-1.02-.2-1.41-.59-.4-.39-.59-.86-.59-1.41v-2c0-.28.1-.52.29-.71.19-.2.43-.29.71-.29.28 0 .52.1.71.29.2.19.29.43.29.71v2h12v-2c0-.28.1-.52.29-.71.19-.2.43-.29.71-.29.28 0 .52.1.71.29.2.19.29.43.29.71v2c0 .55-.2 1.02-.59 1.41-.39.4-.86.59-1.41.59H6Z"></path>',
            more: '<path fill="currentColor" d="M12 8c-.55 0-1.02-.2-1.41-.59C10.2 7.02 10 6.55 10 6s.2-1.02.59-1.41C10.98 4.2 11.45 4 12 4s1.02.2 1.41.59c.39.39.59.86.59 1.41s-.2 1.02-.59 1.41C13.02 7.8 12.55 8 12 8Zm0 6c-.55 0-1.02-.2-1.41-.59C10.2 13.02 10 12.55 10 12s.2-1.02.59-1.41C10.98 10.2 11.45 10 12 10s1.02.2 1.41.59c.39.39.59.86.59 1.41s-.2 1.02-.59 1.41c-.39.39-.86.59-1.41.59Zm0 6c-.55 0-1.02-.2-1.41-.59C10.2 19.02 10 18.55 10 18s.2-1.02.59-1.41C10.98 16.2 11.45 16 12 16s1.02.2 1.41.59c.39.39.59.86.59 1.41s-.2 1.02-.59 1.41c-.39.39-.86.59-1.41.59Z"></path>',
            close: '<path fill="currentColor" d="m12 13.4-4.9 4.9a.95.95 0 0 1-.7.27.95.95 0 0 1-.7-.27.95.95 0 0 1-.28-.7c0-.28.1-.52.28-.7l4.9-4.9-4.9-4.9a.95.95 0 0 1-.28-.7.95.95 0 0 1 .97-.98c.3 0 .53.1.71.28l4.9 4.9 4.9-4.9a.95.95 0 0 1 .7-.27c.28 0 .52.09.7.27.18.18.27.42.27.7 0 .28-.09.52-.27.7L13.4 12l4.9 4.9c.18.18.27.42.27.7 0 .28-.09.52-.27.7a.95.95 0 0 1-.7.27.95.95 0 0 1-.7-.27L12 13.4Z"></path>',
        };
        return `<svg class="wa-viewer-svg" viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none">${icons[name] || ''}</svg>`;
    }

    function safeMediaUrl(value) {
        if (!value) return '';
        const raw = String(value);
        if (/^https?:\/\//i.test(raw)) {
            const hashIndex = raw.indexOf('#');
            const withoutHash = hashIndex >= 0 ? raw.slice(0, hashIndex) : raw;
            const hash = hashIndex >= 0 ? raw.slice(hashIndex) : '';
            const queryIndex = withoutHash.indexOf('?');
            const pathPart = queryIndex >= 0 ? withoutHash.slice(0, queryIndex) : withoutHash;
            const queryPart = queryIndex >= 0 ? withoutHash.slice(queryIndex) : '';
            try {
                return encodeURI(pathPart) + queryPart + hash;
            } catch (e) {
                return raw;
            }
        }
        try {
            return encodeURI(raw);
        } catch (e) {
            return raw;
        }
    }

    function mediaFallbackHtml(url, label = 'Open media') {
        const safeUrl = escapeHtml(url);
        return `
            <a class="wa-media-document wa-media-fallback" href="${safeUrl}" target="_blank" rel="noopener">
                <i class="fa fa-external-link"></i>
                <span>${escapeHtml(label)}</span>
                <i class="fa fa-download"></i>
            </a>
        `;
    }

    function renderMessageContent(row) {
        const contentType = row.content_type || 'Text';
        const mediaUrl = safeMediaUrl(row.attachment_url || row.media_url || '');
        const previewUrl = safeMediaUrl(row.media_proxy_url || row.attachment_url || row.media_url || '');
        const rawBody = row.body || '';
        const body = ['none', 'null', 'undefined'].includes(String(rawBody).trim().toLowerCase()) ? '' : rawBody;
        const safeUrl = escapeHtml(mediaUrl);
        const safePreviewUrl = escapeHtml(previewUrl);
        const safeBody = escapeHtml(body);
        const transport = parseJson(row.raw_transport_payload);

        if (!previewUrl) {
            return `<div class="wa-message-body">${safeBody}</div>`;
        }

        let mediaHtml = '';
        if (contentType === 'Image') {
            mediaHtml = `
                <a class="wa-media-image-link" href="${safePreviewUrl}" data-media-url="${safePreviewUrl}" data-media-caption="${safeBody}" data-media-time="${escapeHtml(formatMessageTime(row.creation))}">
                    <img
                        class="wa-media-image"
                        src="${safePreviewUrl}"
                        alt="${safeBody || 'Image message'}"
                        loading="lazy"
                    />
                </a>
                <template>${mediaFallbackHtml(mediaUrl, safeBody || 'Open image')}</template>
            `;
        } else if (contentType === 'Video') {
            mediaHtml = `
                <video class="wa-media-video" src="${safePreviewUrl}" controls preload="metadata"></video>
                <template>${mediaFallbackHtml(mediaUrl, safeBody || 'Open video')}</template>
            `;
        } else if (contentType === 'Audio') {
            mediaHtml = `
                <audio class="wa-media-audio" src="${safePreviewUrl}" controls preload="metadata"></audio>
                <template>${mediaFallbackHtml(mediaUrl, safeBody || 'Open audio')}</template>
            `;
        } else if (contentType === 'Document') {
            const fileName = row.attachment_file_name || transport.file_name || extractNestedValue(transport, ['fileName', 'file_name']) || extractFileName(mediaUrl, '') || 'Document';
            const fileExt = getFileExtension(fileName) || 'FILE';
            const fileSize = transport.file_size || row.file_size || '';
            mediaHtml = `
                <a class="wa-media-document-card" href="${safeUrl}" target="_blank" rel="noopener">
                    <span class="wa-doc-icon ${fileExt.toLowerCase()}">${documentIcon(fileExt)}</span>
                    <span class="wa-doc-info">
                        <span class="wa-doc-name">${escapeHtml(fileName)}</span>
                        <span class="wa-doc-meta">${escapeHtml(fileExt)}${fileSize ? ` - ${escapeHtml(fileSize)}` : ''}</span>
                    </span>
                </a>
            `;
        } else {
            mediaHtml = `
                <a class="wa-media-document" href="${safeUrl}" target="_blank" rel="noopener">
                    <i class="fa fa-file-text-o"></i>
                    <span>${safeBody || `${escapeHtml(contentType)} attachment`}</span>
                    <i class="fa fa-external-link"></i>
                </a>
            `;
        }

        const caption = body && !isGenericCaption(contentType, body)
            ? `<div class="wa-message-body wa-media-caption">${safeBody}</div>`
            : '';
        return `${mediaHtml}${caption}`;
    }

    function isGenericCaption(contentType, text) {
        const normalizedType = String(contentType || '').trim().toLowerCase();
        const normalizedText = String(text || '').trim().toLowerCase();
        if (!normalizedText || normalizedText === 'none') return true;
        if (normalizedType === 'image') {
            return normalizedText === '[image message received]' || normalizedText === 'image message received' || normalizedText === 'photo';
        }
        return false;
    }

    function parseJson(value) {
        if (!value) return {};
        if (typeof value === 'object') return value;
        try {
            return JSON.parse(value);
        } catch (e) {
            return {};
        }
    }

    function extractNestedValue(value, keys) {
        if (!value || typeof value !== 'object') return '';
        for (const key of keys) {
            if (value[key]) return value[key];
        }
        for (const nested of Object.values(value)) {
            if (nested && typeof nested === 'object') {
                const found = extractNestedValue(nested, keys);
                if (found) return found;
            }
        }
        return '';
    }

    function extractFileName(url, fallback) {
        const cleanFallback = String(fallback || '').trim();
        try {
            const parsed = new URL(url);
            const pathName = decodeURIComponent(parsed.pathname || '');
            const name = pathName.split('/').filter(Boolean).pop();
            if (name && /\.[a-z0-9]{2,8}$/i.test(name)) return name;
        } catch (e) {
            const cleanUrl = String(url || '').split('?')[0];
            const name = decodeURIComponent(cleanUrl.split('/').filter(Boolean).pop() || '');
            if (name && /\.[a-z0-9]{2,8}$/i.test(name)) return name;
        }
        return cleanFallback;
    }

    function getFileExtension(fileName) {
        const match = String(fileName || '').match(/\.([a-z0-9]{2,8})$/i);
        return match ? match[1].toUpperCase() : '';
    }

    function documentIcon(fileExt) {
        if (String(fileExt || '').toLowerCase() === 'pdf') {
            return `${appIcon('pdf')}<span class="wa-pdf-label">PDF</span>`;
        }
        return `<span class="wa-doc-ext">${escapeHtml(fileExt || 'FILE')}</span>`;
    }

    function appIcon(name) {
        const icons = {
            emoji: '<svg viewBox="0 0 24 24" height="24" width="24" preserveAspectRatio="xMidYMid meet" fill="currentColor" aria-hidden="true" focusable="false"><path d="M8.49893 10.2521C9.32736 10.2521 9.99893 9.5805 9.99893 8.75208C9.99893 7.92365 9.32736 7.25208 8.49893 7.25208C7.6705 7.25208 6.99893 7.92365 6.99893 8.75208C6.99893 9.5805 7.6705 10.2521 8.49893 10.2521Z"></path><path d="M17.0011 8.75208C17.0011 9.5805 16.3295 10.2521 15.5011 10.2521C14.6726 10.2521 14.0011 9.5805 14.0011 8.75208C14.0011 7.92365 14.6726 7.25208 15.5011 7.25208C16.3295 7.25208 17.0011 7.92365 17.0011 8.75208Z"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M16.8221 19.9799C15.5379 21.2537 13.8087 21.9781 12 22H9.27273C5.25611 22 2 18.7439 2 14.7273V9.27273C2 5.25611 5.25611 2 9.27273 2H14.7273C18.7439 2 22 5.25611 22 9.27273V11.8141C22 13.7532 21.2256 15.612 19.8489 16.9776L16.8221 19.9799ZM14.7273 4H9.27273C6.36068 4 4 6.36068 4 9.27273V14.7273C4 17.6393 6.36068 20 9.27273 20H11.3331C11.722 19.8971 12.0081 19.5417 12.0058 19.1204L11.9935 16.8564C11.9933 16.8201 11.9935 16.784 11.9941 16.7479C11.0454 16.7473 10.159 16.514 9.33502 16.0479C8.51002 15.5812 7.84752 14.9479 7.34752 14.1479C7.24752 13.9479 7.25585 13.7479 7.37252 13.5479C7.48919 13.3479 7.66419 13.2479 7.89752 13.2479L13.5939 13.2479C14.4494 12.481 15.5811 12.016 16.8216 12.0208L19.0806 12.0296C19.5817 12.0315 19.9889 11.6259 19.9889 11.1248V9.07648H19.9964C19.8932 6.25535 17.5736 4 14.7273 4ZM14.0057 19.1095C14.0066 19.2605 13.9959 19.4089 13.9744 19.5537C14.5044 19.3124 14.9926 18.9776 15.4136 18.5599L18.4405 15.5576C18.8989 15.1029 19.2653 14.5726 19.5274 13.996C19.3793 14.0187 19.2275 14.0301 19.0729 14.0295L16.8138 14.0208C15.252 14.0147 13.985 15.2837 13.9935 16.8455L14.0057 19.1095Z"></path></svg>',
            attach: '<svg viewBox="0 0 24 24" height="24" width="24" preserveAspectRatio="xMidYMid meet" fill="none" aria-hidden="true" focusable="false"><path d="M11 13H5.5C4.94772 13 4.5 12.5523 4.5 12C4.5 11.4477 4.94772 11 5.5 11H11V5.5C11 4.94772 11.4477 4.5 12 4.5C12.5523 4.5 13 4.94772 13 5.5V11H18.5C19.0523 11 19.5 11.4477 19.5 12C19.5 12.5523 19.0523 13 18.5 13H13V18.5C13 19.0523 12.5523 19.5 12 19.5C11.4477 19.5 11 19.0523 11 18.5V13Z" fill="currentColor"></path></svg>',
            emoji: '<svg viewBox="0 0 24 24" height="24" width="24" preserveAspectRatio="xMidYMid meet" fill="currentColor" aria-hidden="true" focusable="false"><path d="M8.49893 10.2521C9.32736 10.2521 9.99893 9.5805 9.99893 8.75208C9.99893 7.92365 9.32736 7.25208 8.49893 7.25208C7.6705 7.25208 6.99893 7.92365 6.99893 8.75208C6.99893 9.5805 7.6705 10.2521 8.49893 10.2521Z"></path><path d="M17.0011 8.75208C17.0011 9.5805 16.3295 10.2521 15.5011 10.2521C14.6726 10.2521 14.0011 9.5805 14.0011 8.75208C14.0011 7.92365 14.6726 7.25208 15.5011 7.25208C16.3295 7.25208 17.0011 7.92365 17.0011 8.75208Z"></path><path fill-rule="evenodd" clip-rule="evenodd" d="M16.8221 19.9799C15.5379 21.2537 13.8087 21.9781 12 22H9.27273C5.25611 22 2 18.7439 2 14.7273V9.27273C2 5.25611 5.25611 2 9.27273 2H14.7273C18.7439 2 22 5.25611 22 9.27273V11.8141C22 13.7532 21.2256 15.612 19.8489 16.9776L16.8221 19.9799ZM14.7273 4H9.27273C6.36068 4 4 6.36068 4 9.27273V14.7273C4 17.6393 6.36068 20 9.27273 20H11.3331C11.722 19.8971 12.0081 19.5417 12.0058 19.1204L11.9935 16.8564C11.9933 16.8201 11.9935 16.784 11.9941 16.7479C11.0454 16.7473 10.159 16.514 9.33502 16.0479C8.51002 15.5812 7.84752 14.9479 7.34752 14.1479C7.24752 13.9479 7.25585 13.7479 7.37252 13.5479C7.48919 13.3479 7.66419 13.2479 7.89752 13.2479L13.5939 13.2479C14.4494 12.481 15.5811 12.016 16.8216 12.0208L19.0806 12.0296C19.5817 12.0315 19.9889 11.6259 19.9889 11.1248V9.07648H19.9964C19.8932 6.25535 17.5736 4 14.7273 4ZM14.0057 19.1095C14.0066 19.2605 13.9959 19.4089 13.9744 19.5537C14.5044 19.3124 14.9926 18.9776 15.4136 18.5599L18.4405 15.5576C18.8989 15.1029 19.2653 14.5726 19.5274 13.996C19.3793 14.0187 19.2275 14.0301 19.0729 14.0295L16.8138 14.0208C15.252 14.0147 13.985 15.2837 13.9935 16.8455L14.0057 19.1095Z"></path></svg>',
            pdf: '<svg viewBox="0 0 22 26" width="26" preserveAspectRatio="xMidYMid meet" fill="none" aria-hidden="true" focusable="false"><path fill="currentColor" d="M1 5.8c0-1.68 0-2.52.327-3.162a3 3 0 0 1 1.311-1.311C3.28 1 4.12 1 5.8 1h5.549c.978 0 1.468 0 1.928.11.408.099.798.26 1.156.48.404.247.75.593 1.441 1.285L17 4l2.125 2.125c.692.692 1.038 1.038 1.286 1.442a4 4 0 0 1 .479 1.156c.11.46.11.95.11 1.928V20.2c0 1.68 0 2.52-.327 3.162a3 3 0 0 1-1.311 1.311C18.72 25 17.88 25 16.2 25H5.8c-1.68 0-2.52 0-3.162-.327a3 3 0 0 1-1.311-1.311C1 22.72 1 21.88 1 20.2V5.8Z"></path><path stroke="#fff" stroke-opacity="0.15" stroke-width="0.5" d="m21.133 8.665-.243.058.243-.058a4.25 4.25 0 0 0-.51-1.229c-.262-.429-.628-.794-1.294-1.46l-.027-.027-2.125-2.126-1.126-1.125-.027-.027c-.666-.666-1.031-1.032-1.46-1.295a4.251 4.251 0 0 0-1.229-.509C12.846.75 12.33.75 11.387.75H5.788c-.83 0-1.468 0-1.979.042-.515.042-.922.128-1.284.312a3.25 3.25 0 0 0-1.42 1.42c-.185.363-.271.77-.313 1.285C.75 4.32.75 4.96.75 5.79v14.423c0 .83 0 1.468.042 1.979.042.515.128.922.312 1.285a3.25 3.25 0 0 0 1.42 1.42c.363.184.77.27 1.285.312.51.042 1.15.042 1.98.042h10.423c.83 0 1.468 0 1.979-.042.515-.042.922-.128 1.285-.312a3.25 3.25 0 0 0 1.42-1.42c.184-.363.27-.77.312-1.285.042-.51.042-1.15.042-1.98V10.614c0-.942 0-1.459-.117-1.948Z"></path><path fill="#fff" fill-opacity="0.4" fill-rule="evenodd" d="m20.71 8.092-5.975-.026a1 1 0 0 1-.995-1.004l.025-5.81c.219.086.43.19.631.314.4.244.744.586 1.43 1.268L17 3.999l2.143 2.13c.701.696 1.052 1.044 1.302 1.452.101.164.19.335.266.511Z" clip-rule="evenodd"></path></svg>',
        };
        return icons[name] || '';
    }

    function formatMessageTime(value) {
        if (!value) return '';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) {
            const text = String(value);
            return text.length > 10 ? text.slice(11, 16) : text;
        }
        return date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
    }

    function renderMessageFooter(row) {
        const time = formatMessageTime(row.creation);
        const statusHtml = row.direction === 'Outbound'
            ? renderStatusTicks(row.delivery_status)
            : '';
        return `<div class="wa-message-footer">${time}${statusHtml}</div>`;
    }

    function renderStatusTicks(status) {
        const normalized = status || 'Pending';
        if (normalized === 'Failed') {
            return `<span class="wa-message-ticks failed" title="${escapeHtml(normalized)}">!</span>`;
        }
        const doubleClass = ['Delivered', 'Read'].includes(normalized) ? 'double' : 'single';
        return `
            <span class="wa-message-ticks ${normalized.toLowerCase()} ${doubleClass}" title="${escapeHtml(normalized)}">
                <svg viewBox="0 0 18 12" aria-hidden="true" focusable="false">
                    <path class="tick-one" d="M1.2 6.4 4.6 9.8 11.7 2.2"></path>
                    <path class="tick-two" d="M6.1 6.4 9.5 9.8 16.6 2.2"></path>
                </svg>
            </span>
        `;
    }

    function updateMessageStatus(data) {
        if (!data || data.conversation !== currentConversation || !data.message) return false;
        const message = $(`.wa-message[data-message="${data.message}"]`);
        if (!message.length) return false;
        message.find('.wa-message-ticks').replaceWith(renderStatusTicks(data.delivery_status));
        return true;
    }

    function renderMessages(rows) {
        const html = rows.length ? rows.map(row => `
            <div class="wa-message ${row.direction === 'Outbound' ? 'outbound' : 'inbound'}" data-message="${row.name}">
                ${renderMessageContent(row)}
                ${renderMessageFooter(row)}
            </div>
        `).join('') : '<div class="wa-empty">No messages.</div>';
        $('#wa-message-list').html(html);
        bindMediaViewerLinks();
        $('#wa-message-list .wa-media-image').on('error', function() {
            const link = this.closest('.wa-media-image-link');
            const fallback = link && link.nextElementSibling;
            if (link && fallback && fallback.tagName === 'TEMPLATE') {
                link.outerHTML = fallback.innerHTML;
            }
        });
        $('#wa-message-list .wa-media-video, #wa-message-list .wa-media-audio').on('error', function() {
            const fallback = this.nextElementSibling;
            if (fallback && fallback.tagName === 'TEMPLATE') {
                this.outerHTML = fallback.innerHTML;
            }
        });
        const messageList = document.getElementById('wa-message-list');
        if (messageList) {
            messageList.scrollTop = messageList.scrollHeight;
        }
    }

    let mediaViewerIndex = 0;
    let mediaViewerItems = [];
    let mediaViewerScale = 1;

    function collectMediaViewerItems() {
        return $('#wa-message-list .wa-media-image-link').map(function() {
            return {
                url: $(this).attr('data-media-url') || $(this).attr('href'),
                caption: $(this).attr('data-media-caption') || 'Image',
                time: $(this).attr('data-media-time') || ''
            };
        }).get();
    }

    function bindMediaViewerLinks() {
        $('#wa-message-list .wa-media-image-link').off('click.waMediaViewer').on('click.waMediaViewer', function(e) {
            e.preventDefault();
            mediaViewerItems = collectMediaViewerItems();
            const clickedUrl = $(this).attr('data-media-url') || $(this).attr('href');
            const index = mediaViewerItems.findIndex(item => item.url === clickedUrl);
            openMediaViewer(index >= 0 ? index : 0);
        });
    }

    function openMediaViewer(index) {
        if (!mediaViewerItems.length) return;
        mediaViewerIndex = Math.max(0, Math.min(index, mediaViewerItems.length - 1));
        mediaViewerScale = 1;
        renderMediaViewer();
        $('#wa-media-viewer').addClass('show').attr('aria-hidden', 'false');
        $('body').addClass('wa-media-viewer-open');
    }

    function closeMediaViewer() {
        $('#wa-media-viewer').removeClass('show').attr('aria-hidden', 'true');
        $('body').removeClass('wa-media-viewer-open');
    }

    function moveMediaViewer(delta) {
        if (!mediaViewerItems.length) return;
        mediaViewerIndex = (mediaViewerIndex + delta + mediaViewerItems.length) % mediaViewerItems.length;
        mediaViewerScale = 1;
        renderMediaViewer();
    }

    function zoomMediaViewer(delta) {
        if (!$('#wa-media-viewer').hasClass('show')) return;
        mediaViewerScale = Math.max(0.5, Math.min(4, Number((mediaViewerScale + delta).toFixed(2))));
        applyMediaViewerZoom();
    }

    function applyMediaViewerZoom() {
        $('#wa-media-viewer-image').css('transform', `scale(${mediaViewerScale})`);
        $('#wa-media-viewer-zoom-out').prop('disabled', mediaViewerScale <= 0.5).toggleClass('disabled', mediaViewerScale <= 0.5);
        $('#wa-media-viewer-zoom-in').prop('disabled', mediaViewerScale >= 4).toggleClass('disabled', mediaViewerScale >= 4);
    }

    function renderMediaViewer() {
        const item = mediaViewerItems[mediaViewerIndex];
        if (!item) return;
        $('#wa-media-viewer-image').attr('src', item.url);
        $('#wa-media-viewer-title').text(item.caption || 'Image');
        $('#wa-media-viewer-subtitle').text(item.time || '');
        $('#wa-media-viewer-open').attr('data-url', item.url);
        $('#wa-media-viewer-download').attr('data-url', item.url);
        $('#wa-media-viewer-prev, #wa-media-viewer-next').toggle(mediaViewerItems.length > 1);

        const thumbs = mediaViewerItems.map((thumb, index) => `
            <button class="wa-media-viewer-thumb ${index === mediaViewerIndex ? 'active' : ''}" type="button" data-index="${index}">
                <img src="${escapeHtml(thumb.url)}" alt="${escapeHtml(thumb.caption || 'Image')}" />
            </button>
        `).join('');
        $('#wa-media-viewer-strip').html(thumbs).toggle(mediaViewerItems.length > 1);
        applyMediaViewerZoom();
    }

    function formatWindowExpiry(isoValue) {
        if (!isoValue) return '';
        const date = new Date(isoValue);
        if (Number.isNaN(date.getTime())) return isoValue;
        return date.toLocaleString([], {
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
        });
    }

    function messagingWindowLabel(windowState) {
        if (!windowState) return '';
        const reason = windowState.reason || '';
        if (reason === 'customer_replied_within_24h') {
            return __('24-hour customer service window');
        }
        if (reason === 'ctwa_72h') {
            return __('72-hour Click-to-WhatsApp window');
        }
        if (reason === 'customer_service_or_ctwa_active') {
            return __('Messaging window active');
        }
        return __('Messaging window');
    }

    function applyMessagingWindowUI(windowState) {
        messagingWindowCache = windowState || null;
        const $banner = $('#wa-messaging-window-banner');
        const canSendFreeForm = !!(windowState && windowState.can_send_free_form);
        const $composer = $('#wa-composer');
        const $body = $('#wa-composer-body');
        const $send = $('#wa-send-btn');
        const $attach = $('#wa-attach-btn');
        const $template = $('#wa-template-btn');

        if (!currentConversation || !windowState) {
            $banner.addClass('is-hidden').removeClass('is-open is-closed').text('');
            $composer.removeClass('is-template-only');
            $template.removeClass('is-emphasized');
            $body.prop('disabled', false);
            $send.prop('disabled', false);
            $attach.prop('disabled', false);
            return;
        }

        if (canSendFreeForm) {
            const expires = formatWindowExpiry(windowState.free_form_expires_at);
            const label = messagingWindowLabel(windowState);
            $banner
                .removeClass('is-hidden is-closed')
                .addClass('is-open')
                .html(`<strong>${escapeHtml(label)}</strong>${expires ? ` &mdash; ${__('until')} ${escapeHtml(expires)}` : ''}`);
            $composer.removeClass('is-template-only');
            $template.removeClass('is-emphasized');
            $body.prop('disabled', false).attr('placeholder', __('Type a message'));
            $send.prop('disabled', false);
            $attach.prop('disabled', false);
        } else {
            $banner
                .removeClass('is-hidden is-open')
                .addClass('is-closed')
                .html(`<strong>${__('Outside messaging window')}</strong> &mdash; ${__('Use Template to send an approved WhatsApp message.')}`);
            $composer.addClass('is-template-only');
            $template.addClass('is-emphasized');
            $body.prop('disabled', true).attr('placeholder', __('Template required — free text not allowed'));
            $send.prop('disabled', true);
            $attach.prop('disabled', true);
            $('#wa-attach-menu').removeClass('show');
        }
    }

    function renderMessagingWindowMeta(windowState) {
        if (!windowState) return '';
        const rows = [];
        if (windowState.can_send_free_form && windowState.free_form_expires_at) {
            rows.push(`<div class="wa-meta-row"><span>${__('Free messaging until')}</span><strong>${escapeHtml(formatWindowExpiry(windowState.free_form_expires_at))}</strong></div>`);
        } else {
            rows.push(`<div class="wa-meta-row"><span>${__('Free messaging')}</span><strong>${__('Closed — use Template')}</strong></div>`);
        }
        if (windowState.last_customer_message_at) {
            rows.push(`<div class="wa-meta-row"><span>${__('Last customer message')}</span><strong>${escapeHtml(formatWindowExpiry(windowState.last_customer_message_at))}</strong></div>`);
        }
        if (windowState.customer_service_active && windowState.customer_service_expires_at) {
            rows.push(`<div class="wa-meta-row"><span>${__('24h window until')}</span><strong>${escapeHtml(formatWindowExpiry(windowState.customer_service_expires_at))}</strong></div>`);
        } else if (windowState.customer_service_expires_at && !windowState.customer_service_active) {
            rows.push(`<div class="wa-meta-row"><span>${__('24h window')}</span><strong>${__('Expired')} ${escapeHtml(formatWindowExpiry(windowState.customer_service_expires_at))}</strong></div>`);
        }
        if (windowState.ctwa_active && windowState.ctwa_expires_at) {
            rows.push(`<div class="wa-meta-row"><span>${__('CTWA 72h until')}</span><strong>${escapeHtml(formatWindowExpiry(windowState.ctwa_expires_at))}</strong></div>`);
        } else if (windowState.ctwa_clid && windowState.ctwa_expires_at && !windowState.ctwa_active) {
            rows.push(`<div class="wa-meta-row"><span>${__('CTWA 72h')}</span><strong>${__('Expired')} ${escapeHtml(formatWindowExpiry(windowState.ctwa_expires_at))}</strong></div>`);
        }
        if (windowState.last_template_sent_at) {
            rows.push(`<div class="wa-meta-row"><span>${__('Last template')}</span><strong>${escapeHtml(formatWindowExpiry(windowState.last_template_sent_at))}${windowState.last_template_category ? ` (${escapeHtml(windowState.last_template_category)})` : ''}</strong></div>`);
        }
        if (!rows.length) return '';
        return `<div class="wa-card-title" style="margin-top:12px">${__('Messaging Window')}</div>${rows.join('')}`;
    }

    function renderContext(data) {
        sidebarContextCache = data || null;
        messagingWindowCache = (data && data.messaging_window) || messagingWindowCache;
        applyMessagingWindowUI(messagingWindowCache);
        const c = data.contact || {};
        const v = data.conversation || {};
        const attribution = data.attribution || {};
        const windowMeta = renderMessagingWindowMeta(data.messaging_window);
        const displayName = c.display_name || c.phone_number || 'Thread';
        const displayPhone = formatPhoneNumber(c.phone_number);
        $('#wa-context-card').html(`
            <div class="wa-profile-card">
                <div class="wa-profile-avatar">${getAvatarText({contact_display_name: displayName})}</div>
                <div class="wa-profile-name">${escapeHtml(displayName)}</div>
                <div class="wa-profile-phone">${escapeHtml(displayPhone || '')}</div>
            </div>
            <div class="wa-meta-row"><span>Name</span><strong>${formatMetaValue(c.display_name)}</strong></div>
            <div class="wa-meta-row"><span>Phone</span><strong>${formatMetaValue(displayPhone)}</strong></div>
            <div class="wa-meta-row"><span>Lead</span><strong>${formatMetaValue(c.linked_lead)}</strong></div>
            <div class="wa-meta-row"><span>Patient</span><strong>${formatMetaValue(c.linked_patient)}</strong></div>
            <div class="wa-meta-row"><span>Assigned</span><strong>${formatMetaValue(v.assigned_to)}</strong></div>
            <div class="wa-meta-row"><span>Department</span><strong>${formatMetaValue(v.department)}</strong></div>
            ${windowMeta}
            ${renderAttribution(attribution)}
        `);
        $('#wa-thread-avatar').text(getAvatarText({contact_display_name: displayName}));
        $('#wa-thread-title').text(displayName);
        $('#wa-thread-subtitle').text(`${v.department || 'No department'} - ${v.status || 'Open'}`);
    }

    function renderAttribution(attribution) {
        if (!attribution || !Object.values(attribution).some(Boolean)) return '';
        const sourceUrl = attribution.source_url || '';
        return `
            <div class="wa-more-wrap">
                <button class="wa-show-more" id="wa-source-toggle" type="button">
                    <span>Show More</span>
                    <i class="fa fa-chevron-down"></i>
                </button>
            </div>
            <div class="wa-attribution-card" id="wa-source-card">
                <div class="wa-card-title">Ad Source</div>
                <div class="wa-source-row"><span>Source ID</span><strong>${formatSourceValue(attribution.source_id)}</strong></div>
                <div class="wa-source-row"><span>Source URL</span><strong>${
                    sourceUrl
                        ? `<a href="${escapeHtml(sourceUrl)}" target="_blank" rel="noopener">${escapeHtml(sourceUrl)}</a>`
                        : '&mdash;'
                }</strong></div>
                <div class="wa-source-row"><span>Source</span><strong>${formatSourceValue(attribution.source)}</strong></div>
                <div class="wa-source-row"><span>ctwa_clid</span><strong>${formatSourceValue(attribution.ctwa_clid)}</strong></div>
            </div>
        `;
    }

    function formatSourceValue(value) {
        return value ? escapeHtml(value) : '&mdash;';
    }

    function formatMetaValue(value) {
        return value ? escapeHtml(value) : '&mdash;';
    }

    function formatPhoneNumber(value) {
        const digits = String(value || '').replace(/\D/g, '');
        if (!digits) return '';
        if (digits.length === 12 && digits.startsWith('91')) {
            return `+91 ${digits.slice(2)}`;
        }
        if (digits.length > 10) {
            return `+${digits.slice(0, digits.length - 10)} ${digits.slice(-10)}`;
        }
        return digits;
    }

    function openContextDrawer() {
        $('#wa-right-pane').removeClass('is-empty');
        $('#wa-chat-hub-layout').removeClass('is-thread-empty');
    }

    function closeContextDrawer() {
        $('#wa-right-pane').addClass('is-empty');
        $('#wa-chat-hub-layout').addClass('is-thread-empty');
    }

    function loadConversation(name) {
        if (conversationLoadInFlightName === name) {
            return;
        }
        const loadToken = ++conversationLoadToken;
        conversationLoadInFlightName = name;
        currentConversation = name;
        $('#wa-center-pane').removeClass('is-empty');
        closeContextDrawer();
        $('.wa-conversation-item').removeClass('active');
        $(`.wa-conversation-item[data-name="${name}"]`).addClass('active');

        const messagesPromise = Promise.resolve(api.messages(name)).then(r => {
            if (loadToken === conversationLoadToken) {
                renderMessages(r.message.result || []);
            }
        });
        const contextPromise = Promise.resolve(api.context(name)).then(r => {
            if (loadToken === conversationLoadToken) {
                renderContext(r.message.result || {});
            }
        });
        const markReadPromise = Promise.resolve(api.markRead(name)).then(() => {
            refreshConversations();
            if (window.wa_chat_hub && wa_chat_hub.notifications && wa_chat_hub.notifications.refresh_count) {
                wa_chat_hub.notifications.refresh_count();
            }
        });

        Promise.allSettled([messagesPromise, contextPromise, markReadPromise]).finally(() => {
            if (loadToken === conversationLoadToken) {
                conversationLoadInFlightName = null;
            }
        });
    }

    function closeCurrentConversation() {
        currentConversation = null;
        messagingWindowCache = null;
        applyMessagingWindowUI(null);
        $('#wa-center-pane').addClass('is-empty');
        $('#wa-chat-hub-layout').addClass('is-thread-empty');
        $('.wa-conversation-item').removeClass('active');
        $('#wa-thread-title').text('Thread');
        $('#wa-thread-subtitle').text('Select a conversation to inspect message history.');
        $('#wa-thread-avatar').text('?');
        $('#wa-message-list').html(`
            <div class="wa-thread-empty-state">
                <div class="wa-thread-empty-icon"><i class="fa fa-whatsapp"></i></div>
                <div class="wa-thread-empty-title">WA Chat Hub</div>
                <div class="wa-thread-empty-text">Select a conversation to view messages and reply.</div>
            </div>
        `);
        $('#wa-context-card').html('<div class="wa-empty">Select a conversation to view context.</div>');
        $('#wa-ai-output').text('No AI output yet.');
        $('#wa-composer-body').val('');
        closeContextDrawer();
        resizeComposer();
    }

    function refreshCurrentConversation() {
        if (!currentConversation) return;
        loadConversation(currentConversation);
    }

    function scheduleConversationRefresh(waitMs) {
        if (!isWaChatHubCurrentRoute()) {
            conversationRefreshState.pending = false;
            clearTimeout(conversationRefreshState.timer);
            return;
        }
        if (document.hidden) {
            conversationRefreshState.pending = true;
            return;
        }
        clearTimeout(conversationRefreshState.timer);
        conversationRefreshState.timer = setTimeout(refreshConversations, waitMs || CONVERSATION_REFRESH_DEBOUNCE_MS);
    }

    function bindRealtime() {
        if (wrapper.wa_chat_hub_realtime_bound) {
            return;
        }
        wrapper.wa_chat_hub_realtime_bound = true;

        frappe.realtime.on('wa_chat_new_message', function(data) {
            if (!isWaChatHubCurrentRoute()) return;
            scheduleConversationRefresh();
            if (isWaChatHubRouteActive() && data && data.conversation === currentConversation) {
                refreshCurrentConversation();
            }
        });

        frappe.realtime.on('wa_chat_message_status_updated', function(data) {
            if (!isWaChatHubRouteActive()) return;
            if (data && data.conversation === currentConversation) {
                if (!updateMessageStatus(data)) {
                    refreshCurrentConversation();
                }
            }
        });

        frappe.realtime.on('wa_chat_conversation_updated', function(data) {
            if (!isWaChatHubCurrentRoute()) return;
            scheduleConversationRefresh();
            if (isWaChatHubRouteActive() && data && data.conversation === currentConversation) {
                refreshCurrentConversation();
            }
        });

        frappe.realtime.on('wa_chat_window_updated', function(data) {
            if (!isWaChatHubRouteActive()) return;
            if (data && data.conversation === currentConversation && data.messaging_window) {
                messagingWindowCache = data.messaging_window;
                applyMessagingWindowUI(messagingWindowCache);
            }
        });
    }

    $('#wa-ai-summary').on('click', function() {
        if (!currentConversation) return frappe.show_alert({message: 'Select a conversation first', indicator: 'orange'});
        api.aiSummary(currentConversation).then(r => $('#wa-ai-output').text((r.message.result || {}).content || 'No output'));
    });

    $('#wa-ai-draft').on('click', function() {
        if (!currentConversation) return frappe.show_alert({message: 'Select a conversation first', indicator: 'orange'});
        api.aiDraft(currentConversation).then(r => {
            const content = (r.message.result || {}).content || '';
            $('#wa-ai-output').text(content || 'No output');
            $('#wa-composer-body').val(content);
        });
    });

    function parseOutboundSendResult(response) {
        return (response && response.message && response.message.result) || {};
    }

    function notifyOutboundSendOutcome(response, successMessage) {
        const result = parseOutboundSendResult(response);
        const status = String(result.delivery_status || '');
        const errorText = result.error || result.warning || '';
        if (status === 'Failed' || result.sent === false) {
            frappe.msgprint({
                title: __('Message not sent'),
                message: errorText || __(
                    'WhatsApp could not deliver this message. If the customer has not messaged in the last 24 hours, use the Template button instead of free text.'
                ),
                indicator: 'red',
            });
            return false;
        }
        if (errorText) {
            frappe.show_alert({message: errorText, indicator: 'orange'});
        } else {
            frappe.show_alert({message: successMessage, indicator: 'green'});
        }
        return true;
    }

    function sendComposerMessage() {
        const body = ($('#wa-composer-body').val() || '').trim();
        if (!currentConversation) {
            return frappe.show_alert({message: __('Select a conversation first'), indicator: 'orange'});
        }
        if (!body) {
            return frappe.show_alert({message: __('Type a message to send'), indicator: 'orange'});
        }
        if (messagingWindowCache && !messagingWindowCache.can_send_free_form) {
            return frappe.show_alert({
                message: __('Outside messaging window — use Template to send'),
                indicator: 'orange',
            });
        }
        const $btn = $('#wa-send-btn');
        $btn.prop('disabled', true);
        api.sendReply(currentConversation, body).then((r) => {
            const sent = notifyOutboundSendOutcome(r, __('Message sent'));
            if (sent) {
                $('#wa-composer-body').val('');
                resizeComposer();
            }
            loadConversation(currentConversation);
        }).catch((err) => {
            frappe.msgprint({
                title: __('Send failed'),
                message: (err && err.message) || __('Could not send message. Check Interakt API key and Error Log.'),
                indicator: 'red',
            });
            loadConversation(currentConversation);
        }).always(() => {
            $btn.prop('disabled', false);
        });
    }

    $('#wa-send-btn').on('click', function() {
        sendComposerMessage();
    });

    $('#wa-composer-body').on('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendComposerMessage();
        }
    });

    function resizeComposer() {
        const textarea = document.getElementById('wa-composer-body');
        if (!textarea) return;

        const maxHeight = 168;
        const minHeight = 38;
        textarea.style.height = '38px';
        const nextHeight = Math.min(textarea.scrollHeight, maxHeight);
        textarea.style.height = `${Math.max(nextHeight, minHeight)}px`;
        textarea.style.overflowY = textarea.scrollHeight > maxHeight ? 'auto' : 'hidden';
        $('.wa-composer').toggleClass('is-multiline', nextHeight > 56);
    }

    $('#wa-composer-body').on('input', resizeComposer);
    resizeComposer();

    function hideAttachmentMenu() {
        $('#wa-attach-menu').removeClass('show');
    }

    function showUnsupportedAttachment(label) {
        frappe.show_alert({
            message: `${label} sending needs a dedicated Interakt API payload. Added as pending item.`,
            indicator: 'orange'
        });
    }

    function showMediaDialog(config) {
        if (!currentConversation) return frappe.show_alert({message: 'Select a conversation first', indicator: 'orange'});
        const contentTypes = config.options || [config.content_type];
        const dialog = new frappe.ui.Dialog({
            title: config.title || 'Send Media',
            fields: [
                {
                    fieldname: 'content_type',
                    fieldtype: 'Select',
                    label: 'Type',
                    options: contentTypes.join('\n'),
                    default: config.content_type || contentTypes[0],
                    reqd: 1,
                    hidden: contentTypes.length === 1
                },
                {fieldname: 'media_url', fieldtype: 'Data', label: 'Media URL', reqd: 1},
                {fieldname: 'caption', fieldtype: 'Small Text', label: config.caption_label || 'Caption / File Name'}
            ],
            primary_action_label: 'Send',
            primary_action(values) {
                api.sendReply(
                    currentConversation,
                    values.caption || '',
                    values.content_type || config.content_type,
                    values.media_url
                ).then((r) => {
                    const sent = notifyOutboundSendOutcome(r, `${config.label || 'Media'} sent`);
                    if (sent) {
                        dialog.hide();
                    }
                    loadConversation(currentConversation);
                }).catch((err) => {
                    frappe.msgprint({
                        title: __('Send failed'),
                        message: (err && err.message) || __('Could not send media message.'),
                        indicator: 'red',
                    });
                    loadConversation(currentConversation);
                });
            }
        });
        dialog.show();
    }

    function uploadImageFile(file) {
        return uploadMediaFile(file, 'wa_chat_hub.api.runtime.upload_image_for_send', 'Image upload failed');
    }

    function uploadDocumentFile(file) {
        return uploadMediaFile(file, 'wa_chat_hub.api.runtime.upload_document_for_send', 'Document upload failed');
    }

    function uploadMediaFile(file, method, fallbackMessage) {
        const formData = new FormData();
        formData.append('conversation', currentConversation);
        formData.append('file', file);

        return fetch(`/api/method/${method}`, {
            method: 'POST',
            headers: {
                'X-Frappe-CSRF-Token': frappe.csrf_token
            },
            body: formData
        }).then(response => response.json()).then(data => {
            if (data.exc || (data.message && data.message.success === false)) {
                throw new Error((data.message && data.message.message) || data._server_messages || fallbackMessage);
            }
            return (data.message || {}).result || {};
        });
    }

    function showImageUploadDialog(config = {}) {
        if (!currentConversation) return frappe.show_alert({message: 'Select a conversation first', indicator: 'orange'});
        const dialog = new frappe.ui.Dialog({
            title: config.title || 'Send Image',
            fields: [
                {
                    fieldname: 'image_upload',
                    fieldtype: 'HTML',
                    options: '<input type="file" class="form-control" id="wa-image-upload-input" accept="image/*" />'
                },
                {
                    fieldname: 'caption',
                    fieldtype: 'Small Text',
                    label: 'Caption'
                }
            ],
            primary_action_label: 'Upload & Send',
            primary_action(values) {
                const fileInput = dialog.$wrapper.find('#wa-image-upload-input').get(0);
                const file = fileInput && fileInput.files && fileInput.files[0];
                if (!file) {
                    frappe.show_alert({message: 'Choose an image first', indicator: 'orange'});
                    return;
                }
                if (!file.type || !file.type.startsWith('image/')) {
                    frappe.show_alert({message: 'Only image files are supported right now', indicator: 'orange'});
                    return;
                }

                dialog.get_primary_btn().prop('disabled', true).text('Uploading to Interakt...');
                uploadImageFile(file)
                    .then(upload => api.sendMediaReply(
                        currentConversation,
                        values.caption || '',
                        'Image',
                        upload.media_url,
                        upload.file_url,
                        upload.file_name || file.name,
                        upload.file_size,
                        upload.file
                    ))
                    .then((r) => {
                        const sent = notifyOutboundSendOutcome(r, __('Image sent'));
                        if (sent) {
                            dialog.hide();
                        }
                        loadConversation(currentConversation);
                    })
                    .catch(error => {
                        frappe.msgprint({
                            title: __('Image send failed'),
                            message: (error && error.message) || __('Image send failed'),
                            indicator: 'red',
                        });
                        loadConversation(currentConversation);
                    })
                    .finally(() => {
                        dialog.get_primary_btn().prop('disabled', false).text('Upload & Send');
                    });
            }
        });
        dialog.show();
    }

    function showDocumentUploadDialog(config = {}) {
        if (!currentConversation) return frappe.show_alert({message: 'Select a conversation first', indicator: 'orange'});
        const dialog = new frappe.ui.Dialog({
            title: config.title || 'Send Document',
            fields: [
                {
                    fieldname: 'document_upload',
                    fieldtype: 'HTML',
                    options: '<input type="file" class="form-control" id="wa-document-upload-input" accept=".pdf,.txt,.doc,.docx,.xls,.xlsx,.ppt,.pptx,application/pdf,text/plain,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-powerpoint,application/vnd.openxmlformats-officedocument.presentationml.presentation" />'
                },
                {
                    fieldname: 'caption',
                    fieldtype: 'Small Text',
                    label: 'Message / Caption'
                }
            ],
            primary_action_label: 'Upload & Send',
            primary_action(values) {
                const fileInput = dialog.$wrapper.find('#wa-document-upload-input').get(0);
                const file = fileInput && fileInput.files && fileInput.files[0];
                if (!file) {
                    frappe.show_alert({message: 'Choose a document first', indicator: 'orange'});
                    return;
                }

                dialog.get_primary_btn().prop('disabled', true).text('Uploading to Interakt...');
                uploadDocumentFile(file)
                    .then(upload => api.sendMediaReply(
                        currentConversation,
                        values.caption || '',
                        'Document',
                        upload.media_url,
                        upload.file_url,
                        upload.file_name || file.name,
                        upload.file_size,
                        upload.file
                    ))
                    .then((r) => {
                        const sent = notifyOutboundSendOutcome(r, __('Document sent'));
                        if (sent) {
                            dialog.hide();
                        }
                        loadConversation(currentConversation);
                    })
                    .catch(error => {
                        frappe.msgprint({
                            title: __('Document send failed'),
                            message: (error && error.message) || __('Document send failed'),
                            indicator: 'red',
                        });
                        loadConversation(currentConversation);
                    })
                    .finally(() => {
                        dialog.get_primary_btn().prop('disabled', false).text('Upload & Send');
                    });
            }
        });
        dialog.show();
    }

    $('#wa-attach-btn').on('click', function(e) {
        e.stopPropagation();
        if (!currentConversation) return frappe.show_alert({message: 'Select a conversation first', indicator: 'orange'});
        $('#wa-attach-menu').toggleClass('show');
    });

    $(document).on('click.waAttachMenu', function(e) {
        if (!$(e.target).closest('.wa-attach-wrap').length) {
            hideAttachmentMenu();
        }
    });

    $('#wa-attach-menu').on('click', '.wa-attach-item', function(e) {
        e.stopPropagation();
        const action = $(this).data('action');
        hideAttachmentMenu();

        if (action === 'document') {
            showDocumentUploadDialog({title: 'Send Document'});
        } else if (action === 'photos') {
            showImageUploadDialog({title: 'Send Image'});
        } else if (action === 'camera') {
            showImageUploadDialog({title: 'Send Camera Image'});
        } else if (action === 'audio') {
            showMediaDialog({title: 'Send Audio', label: 'Audio', content_type: 'Audio', caption_label: 'Message'});
        } else if (action === 'sticker') {
            showMediaDialog({title: 'Send Sticker', label: 'Sticker', content_type: 'Sticker', caption_label: 'Sticker Name'});
        } else {
            const label = $(this).text().trim();
            showUnsupportedAttachment(label);
        }
    });

    function templateVariableSlots(row, section) {
        const key = section === 'header' ? 'header_variables' : 'body_variables';
        const fromApi = row && Array.isArray(row[key]) ? row[key] : [];
        if (fromApi.length) {
            return fromApi;
        }
        const text = section === 'header'
            ? (row && row.header_preview) || ''
            : (row && row.body_preview) || '';
        let count = section === 'header'
            ? (row && row.header_variable_count) || 0
            : (row && row.body_variable_count) || 0;
        if (!count && section === 'body' && row) {
            const vp = (row.variable_present || '').toString().toLowerCase();
            if (vp === 'yes' || row.has_variables) {
                count = Math.max(count, 1);
            }
        }
        return buildVariableSlotsFromText(text, count, section);
    }

    function buildVariableSlotsFromText(text, expectedCount, section) {
        const indices = [];
        const re = /\{\{(\d+)\}\}/g;
        let match;
        while ((match = re.exec(text || '')) !== null) {
            const idx = parseInt(match[1], 10);
            if (!indices.includes(idx)) {
                indices.push(idx);
            }
        }
        if (!indices.length && expectedCount > 0) {
            for (let i = 1; i <= expectedCount; i += 1) {
                indices.push(i);
            }
        }
        return indices.map((index) => {
            const placeholder = `{{${index}}}`;
            return {
                index,
                placeholder,
                context: variableContextSnippet(text, placeholder),
                section,
            };
        });
    }

    function variableContextSnippet(text, placeholder, radius = 20) {
        if (!text) {
            return placeholder;
        }
        const pos = text.indexOf(placeholder);
        if (pos < 0) {
            return placeholder;
        }
        let snippet = text.slice(Math.max(0, pos - radius), pos + placeholder.length + radius);
        if (pos - radius > 0) {
            snippet = `…${snippet}`;
        }
        if (pos + placeholder.length + radius < text.length) {
            snippet = `${snippet}…`;
        }
        return snippet;
    }

    function templateHasBodyVariables(row) {
        if (!row) return false;
        if (templateVariableSlots(row, 'body').length) return true;
        const vp = (row.variable_present || '').toString().toLowerCase();
        return vp === 'yes' || !!row.has_variables;
    }

    function templateHasHeaderVariables(row) {
        if (!row) return false;
        return templateVariableSlots(row, 'header').length > 0;
    }

    function templateMapFieldOptions() {
        return [
            {value: 'contact.display_name', label: __('Contact name')},
            {value: 'contact.phone_number', label: __('Phone number')},
            {value: 'contact.linked_lead', label: __('Linked lead')},
            {value: 'contact.linked_patient', label: __('Linked patient')},
            {value: 'conversation.department', label: __('Department')},
            {value: 'conversation.assigned_to', label: __('Assigned to')},
        ];
    }

    function resolveTemplateMapValue(path, ctx) {
        if (!path || !ctx) return '';
        const contact = ctx.contact || {};
        const conversation = ctx.conversation || {};
        const map = {
            'contact.display_name': contact.display_name,
            'contact.phone_number': contact.phone_number,
            'contact.linked_lead': contact.linked_lead,
            'contact.linked_patient': contact.linked_patient,
            'conversation.department': conversation.department,
            'conversation.assigned_to': conversation.assigned_to,
        };
        const value = map[path];
        return value == null ? '' : String(value);
    }

    function buildInteraktVariableSectionHtml(title, variables, section, mapOptions) {
        if (!variables || !variables.length) {
            return '';
        }
        const rows = variables.map((slot) => {
            const idx = slot.index;
            const placeholder = slot.placeholder || `{{${idx}}}`;
            const ctx = escapeHtml(slot.context || placeholder);
            const mapOpts = mapOptions.map((opt) => (
                `<option value="${escapeHtml(opt.value)}">${escapeHtml(opt.label)}</option>`
            )).join('');
            return `
                <div class="wa-tpl-var-row" data-wa-var-row="${section}_${idx}">
                    <div class="wa-tpl-var-context" title="${ctx}">${ctx}</div>
                    <div class="wa-tpl-var-input-wrap">
                        <input type="text" class="form-control wa-tpl-var-input"
                            data-wa-var-input="${section}_${idx}"
                            placeholder="${escapeHtml(__('Enter value of'))} ${escapeHtml(placeholder)}" />
                    </div>
                    <div class="wa-tpl-var-or">${__('or')}</div>
                    <div class="wa-tpl-var-map">
                        <select class="form-control wa-tpl-var-map-select" data-wa-var-map="${section}_${idx}">
                            <option value="">${escapeHtml(__('Map a field to'))} ${escapeHtml(placeholder)}</option>
                            ${mapOpts}
                        </select>
                    </div>
                </div>`;
        }).join('');
        return `
            <div class="wa-tpl-var-section" data-wa-var-section="${section}">
                <div class="wa-tpl-var-section-head">
                    <strong>${escapeHtml(title)}</strong>
                </div>
                <div class="wa-tpl-var-rows">${rows}</div>
            </div>`;
    }

    function applyTemplatePreviewWithVariables(dialog, selected) {
        if (!selected || !dialog.fields_dict.preview) return;
        let preview = selected.body_preview || '';
        templateVariableSlots(selected, 'body').forEach((slot) => {
            const key = `body_${slot.index}`;
            const value = (dialog.$wrapper.find(`[data-wa-var-input="${key}"]`).val() || '').trim();
            if (value) {
                preview = preview.split(slot.placeholder).join(value);
            }
        });
        dialog.fields_dict.preview.set_value(preview || selected.body_preview || '');
    }

    function openInteraktTemplateDialog() {
        if (!currentConversation) {
            return frappe.show_alert({message: __('Select a conversation first'), indicator: 'orange'});
        }

        let approvedTemplates = [];
        let dialogContext = sidebarContextCache;
        const mapOptions = templateMapFieldOptions();

        const dialog = new frappe.ui.Dialog({
            title: __('Send Interakt Template'),
            fields: [
                {
                    fieldname: 'template_help',
                    fieldtype: 'HTML',
                    options: `<p class="text-muted" style="margin:0">${__('Choose an approved template from your Interakt account. Templates are managed in Interakt, not here.')}</p>`,
                },
                {
                    fieldname: 'template_key',
                    fieldtype: 'Select',
                    label: __('Approved Template'),
                    reqd: 1,
                    options: [],
                },
                {
                    fieldname: 'language_code',
                    fieldtype: 'Data',
                    label: __('Language Code'),
                    default: 'en',
                    read_only: 1,
                },
                {
                    fieldname: 'body_variables_html',
                    fieldtype: 'HTML',
                    label: '',
                    options: '',
                },
                {
                    fieldname: 'header_variables_html',
                    fieldtype: 'HTML',
                    label: '',
                    options: '',
                },
                {
                    fieldname: 'preview',
                    fieldtype: 'Small Text',
                    label: __('Message Preview'),
                    read_only: 1,
                },
            ],
            primary_action_label: __('Send'),
            primary_action(values) {
                const selected = approvedTemplates.find((row) => row._key === values.template_key);
                if (!selected) {
                    frappe.msgprint(__('Please select a template'));
                    return;
                }
                const bodyValues = collectTemplateVariableValues(dialog, selected, 'body');
                const headerValues = collectTemplateVariableValues(dialog, selected, 'header');
                if (templateHasBodyVariables(selected) && bodyValues.some((v) => !v)) {
                    frappe.msgprint(__('Please enter a value for each body variable'));
                    return;
                }
                if (templateHasHeaderVariables(selected) && headerValues.some((v) => !v)) {
                    frappe.msgprint(__('Please enter a value for each header variable'));
                    return;
                }
                dialog.get_primary_btn().prop('disabled', true);
                api.sendTemplate({
                    conversation: currentConversation,
                    template_name: selected.name,
                    language_code: values.language_code || selected.language_code || 'en',
                    body_values: bodyValues,
                    header_values: headerValues,
                    template_category: selected.category || '',
                    body: values.preview || selected.body_preview || `Template: ${selected.name}`,
                }).then((r) => {
                    const sent = notifyOutboundSendOutcome(r, __('Template sent'));
                    if (sent) {
                        dialog.hide();
                    }
                    loadConversation(currentConversation);
                }).catch((err) => {
                    frappe.msgprint({
                        title: __('Template send failed'),
                        message: (err && err.message) || __('Could not send template via Interakt.'),
                        indicator: 'red',
                    });
                    loadConversation(currentConversation);
                }).always(() => {
                    dialog.get_primary_btn().prop('disabled', false);
                });
            },
        });

        function collectTemplateVariableValues(dlg, row, section) {
            return templateVariableSlots(row, section)
                .sort((a, b) => a.index - b.index)
                .map((slot) => (dlg.$wrapper.find(`[data-wa-var-input="${section}_${slot.index}"]`).val() || '').trim());
        }

        function setVariableSectionHtml(field, html) {
            if (!field) return;
            field.df.options = html || '<div class="wa-tpl-var-empty"></div>';
            field.refresh();
            if (field.$wrapper) {
                field.$wrapper.toggle(!!html);
            }
        }

        function bindTemplateVariableEvents(selected) {
            dialog.$wrapper.off('.waTplVar');
            dialog.$wrapper.on('change.waTplVar', '.wa-tpl-var-map-select', function() {
                const key = $(this).data('wa-var-map');
                const mapped = resolveTemplateMapValue($(this).val(), dialogContext);
                if (mapped) {
                    dialog.$wrapper.find(`[data-wa-var-input="${key}"]`).val(mapped);
                    applyTemplatePreviewWithVariables(dialog, selected);
                }
            });
            dialog.$wrapper.on('input.waTplVar', '.wa-tpl-var-input', function() {
                applyTemplatePreviewWithVariables(dialog, selected);
            });
        }

        function applyTemplateSelection(templateKey) {
            const selected = approvedTemplates.find((row) => row._key === templateKey);
            if (!selected) return;
            dialog.fields_dict.language_code.set_value(selected.language_code || 'en');

            const bodySlots = templateVariableSlots(selected, 'body');
            const headerSlots = templateVariableSlots(selected, 'header');
            const bodyHtml = bodySlots.length
                ? buildInteraktVariableSectionHtml(__('Configure Body Variable'), bodySlots, 'body', mapOptions)
                : '';
            const headerHtml = headerSlots.length
                ? buildInteraktVariableSectionHtml(__('Configure Header Variable'), headerSlots, 'header', mapOptions)
                : '';

            setVariableSectionHtml(dialog.fields_dict.body_variables_html, bodyHtml);
            setVariableSectionHtml(dialog.fields_dict.header_variables_html, headerHtml);
            bindTemplateVariableEvents(selected);
            applyTemplatePreviewWithVariables(dialog, selected);
        }

        const templateField = dialog.fields_dict.template_key;
        templateField.df.onchange = () => applyTemplateSelection(templateField.get_value());

        dialog.show();
        dialog.$wrapper.closest('.modal-dialog').addClass('wa-tpl-dialog-modal');
        dialog.get_primary_btn().prop('disabled', true);
        setVariableSectionHtml(dialog.fields_dict.body_variables_html, '');
        setVariableSectionHtml(dialog.fields_dict.header_variables_html, '');
        if (templateField.$input) {
            templateField.$input.prop('disabled', true);
        }

        const contextPromise = dialogContext
            ? Promise.resolve(dialogContext)
            : api.context(currentConversation).then((r) => {
                dialogContext = (r.message || {}).result || null;
                sidebarContextCache = dialogContext;
                return dialogContext;
            });

        Promise.all([
            contextPromise.catch(() => null),
            api.getInteraktTemplates({conversation: currentConversation}),
        ]).then(([, r]) => {
            const payload = (r.message || {}).result || {};
            approvedTemplates = (payload.templates || []).map((row, index) => {
                const label = row.display_name && row.display_name !== row.name
                    ? `${row.display_name} (${row.name})`
                    : row.name;
                const category = row.category ? ` · ${row.category}` : '';
                return {
                    ...row,
                    _key: `${row.name}::${row.language_code || 'en'}::${index}`,
                    _label: `${label}${category}`,
                };
            });

            if (!approvedTemplates.length) {
                frappe.msgprint({
                    title: __('No approved templates'),
                    message: __('No approved Interakt templates were returned for this channel account. Create and approve templates in Interakt first.'),
                    indicator: 'orange',
                });
                dialog.hide();
                return;
            }

            templateField.df.options = approvedTemplates.map((row) => ({
                label: row._label,
                value: row._key,
            }));
            templateField.refresh();
            templateField.$input.prop('disabled', false);
            templateField.set_value(approvedTemplates[0]._key);
            applyTemplateSelection(approvedTemplates[0]._key);
            dialog.get_primary_btn().prop('disabled', false);
        }).catch((err) => {
            dialog.hide();
            frappe.msgprint({
                title: __('Could not load templates'),
                message: err.message || __('Failed to fetch approved templates from Interakt.'),
                indicator: 'red',
            });
        });
    }

    $('#wa-template-btn').on('click', function() {
        openInteraktTemplateDialog();
    });

    $('#wa-create-lead').on('click', function() {
        if (!currentConversation) return;
        api.createLead(currentConversation).then(r => {
            frappe.show_alert({message: `Lead created: ${(r.message.result || {}).name}`, indicator: 'green'});
            loadConversation(currentConversation);
        });
    });

    $('#wa-create-issue').on('click', function() {
        if (!currentConversation) return;
        api.createIssue(currentConversation).then(r => {
            frappe.show_alert({message: `Issue created: ${(r.message.result || {}).name}`, indicator: 'green'});
            loadConversation(currentConversation);
        });
    });

    $('#wa-media-viewer-close').on('click', closeMediaViewer);
    $('#wa-media-viewer-zoom-out').on('click', function() {
        zoomMediaViewer(-0.25);
    });
    $('#wa-media-viewer-zoom-in').on('click', function() {
        zoomMediaViewer(0.25);
    });
    $('#wa-media-viewer-prev').on('click', function() {
        moveMediaViewer(-1);
    });
    $('#wa-media-viewer-next').on('click', function() {
        moveMediaViewer(1);
    });
    $('#wa-media-viewer-open').on('click', function() {
        const url = $(this).attr('data-url');
        if (url) window.open(url, '_blank', 'noopener');
    });
    $('#wa-media-viewer-download').on('click', function() {
        const url = $(this).attr('data-url');
        if (!url) return;
        const link = document.createElement('a');
        link.href = url;
        link.download = '';
        link.target = '_blank';
        document.body.appendChild(link);
        link.click();
        link.remove();
    });
    $('#wa-media-viewer-strip').on('click', '.wa-media-viewer-thumb', function() {
        openMediaViewer(Number($(this).attr('data-index')) || 0);
    });
    $('#wa-media-viewer-image').on('wheel', function(e) {
        e.preventDefault();
        const event = e.originalEvent;
        zoomMediaViewer(event.deltaY < 0 ? 0.15 : -0.15);
    });
    $('#wa-media-viewer').on('click', function(e) {
        if ($(e.target).is('#wa-media-viewer')) {
            closeMediaViewer();
        }
    });
    $(document).on('keydown.waMediaViewer', function(e) {
        const viewerOpen = $('#wa-media-viewer').hasClass('show');
        if (e.key === 'Escape') {
            if (viewerOpen) {
                closeMediaViewer();
            } else if (!$('#wa-right-pane').hasClass('is-empty')) {
                closeContextDrawer();
            } else if (currentConversation) {
                closeCurrentConversation();
            }
            return;
        }
        if (!viewerOpen) return;
        if (e.key === 'ArrowLeft') moveMediaViewer(-1);
        if (e.key === 'ArrowRight') moveMediaViewer(1);
    });

    document.addEventListener('visibilitychange', function() {
        if (isWaChatHubRouteActive() && conversationRefreshState.pending) {
            conversationRefreshState.pending = false;
            scheduleConversationRefresh(250);
        }
    });

    bindRealtime();
    loadChannelAccounts().always(() => {
        Promise.resolve(consumeRouteConversation()).then((consumed) => {
            if (!consumed) {
                refreshConversations();
            }
        });
    });
};
