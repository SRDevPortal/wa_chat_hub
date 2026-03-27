frappe.pages['wa-chat-hub'].on_page_load = function(wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: 'WA Chat Hub',
        single_column: true
    });

    let currentConversation = null;

    $(wrapper).html(`
        <div class="wa-chat-hub-layout">
            <aside class="wa-left-pane">
                <div class="wa-pane-title">Conversations</div>
                <input class="wa-search" id="wa-search" placeholder="Search phone / patient / lead" />
                <div class="wa-filter-row">
                    <button class="wa-chip active">All</button>
                    <button class="wa-chip">Unassigned</button>
                    <button class="wa-chip">Mine</button>
                </div>
                <div class="wa-conversation-list" id="wa-conversation-list"><div class="wa-empty">Loading...</div></div>
            </aside>
            <main class="wa-center-pane">
                <div class="wa-thread-header">
                    <div>
                        <div class="wa-pane-title" id="wa-thread-title">Thread</div>
                        <div class="wa-thread-subtitle" id="wa-thread-subtitle">Select a conversation to inspect message history.</div>
                    </div>
                    <div class="wa-thread-actions">
                        <button class="btn btn-default btn-sm" id="wa-ai-summary">AI Summary</button>
                        <button class="btn btn-default btn-sm" id="wa-ai-draft">AI Draft</button>
                    </div>
                </div>
                <div class="wa-message-list" id="wa-message-list"><div class="wa-empty">Select a conversation.</div></div>
                <div class="wa-composer">
                    <textarea class="form-control" rows="3" id="wa-composer-body" placeholder="Type a reply..."></textarea>
                    <div class="wa-composer-actions">
                        <button class="btn btn-secondary btn-sm" id="wa-send-btn">Send</button>
                    </div>
                </div>
            </main>
            <aside class="wa-right-pane">
                <div class="wa-pane-title">Context & Actions</div>
                <div class="wa-card" id="wa-context-card"><div class="wa-empty">No conversation selected.</div></div>
                <div class="wa-card">
                    <div class="wa-card-title">ERP Actions</div>
                    <div class="wa-action-list">
                        <button class="btn btn-default btn-sm" id="wa-create-lead">Create Lead</button>
                        <button class="btn btn-default btn-sm" id="wa-create-issue">Create Support Ticket</button>
                    </div>
                </div>
                <div class="wa-card">
                    <div class="wa-card-title">AI Output</div>
                    <pre class="wa-pre" id="wa-ai-output">No AI output yet.</pre>
                </div>
            </aside>
        </div>
    `);

    const api = {
        conversations: () => frappe.call('wa_chat_hub.api.chat.get_conversations', { limit: 100 }),
        messages: (conversation) => frappe.call('wa_chat_hub.api.chat.get_messages', { conversation, limit: 200 }),
        context: (conversation) => frappe.call('wa_chat_hub.api.chat.get_sidebar_context', { conversation }),
        aiSummary: (conversation) => frappe.call('wa_chat_hub.api.ai.summarize_conversation', { conversation }),
        aiDraft: (conversation) => frappe.call('wa_chat_hub.api.ai.draft_reply', { conversation }),
        sendReply: (conversation, body) => frappe.call('wa_chat_hub.api.runtime.send_reply', { conversation, body }),
        createLead: (conversation) => frappe.call('wa_chat_hub.api.actions.create_lead_from_conversation', { conversation }),
        createIssue: (conversation) => frappe.call('wa_chat_hub.api.actions.create_issue_from_conversation', { conversation }),
    };

    function renderConversations(rows) {
        const html = rows.length ? rows.map(row => `
            <button class="wa-conversation-item" data-name="${row.name}">
                <div class="wa-conversation-top">
                    <strong>${row.contact_display_name || row.contact_phone_number || row.name}</strong>
                    <span class="wa-badge">${row.unread_count || 0}</span>
                </div>
                <div class="wa-conversation-meta">${row.department || '—'} • ${row.status || 'Open'}</div>
                <div class="wa-conversation-preview">${frappe.utils.escape_html(row.last_message_preview || '')}</div>
            </button>
        `).join('') : '<div class="wa-empty">No conversations found.</div>';
        $('#wa-conversation-list').html(html);
        $('.wa-conversation-item').on('click', function() {
            loadConversation($(this).data('name'));
        });
    }

    function renderMessages(rows) {
        const html = rows.length ? rows.map(row => `
            <div class="wa-message ${row.direction === 'Outbound' ? 'outbound' : 'inbound'}">
                <div class="wa-message-meta">${row.sender_type || row.direction} • ${row.creation}</div>
                <div class="wa-message-body">${frappe.utils.escape_html(row.body || '')}</div>
            </div>
        `).join('') : '<div class="wa-empty">No messages.</div>';
        $('#wa-message-list').html(html);
    }

    function renderContext(data) {
        const c = data.contact || {};
        const v = data.conversation || {};
        $('#wa-context-card').html(`
            <div class="wa-meta-row"><span>Name</span><strong>${c.display_name || '—'}</strong></div>
            <div class="wa-meta-row"><span>Phone</span><strong>${c.phone_number || '—'}</strong></div>
            <div class="wa-meta-row"><span>Lead</span><strong>${c.linked_lead || '—'}</strong></div>
            <div class="wa-meta-row"><span>Patient</span><strong>${c.linked_patient || '—'}</strong></div>
            <div class="wa-meta-row"><span>Assigned</span><strong>${v.assigned_to || '—'}</strong></div>
            <div class="wa-meta-row"><span>Department</span><strong>${v.department || '—'}</strong></div>
        `);
        $('#wa-thread-title').text(c.display_name || c.phone_number || 'Thread');
        $('#wa-thread-subtitle').text(`${v.department || 'No department'} • ${v.status || 'Open'}`);
    }

    function loadConversation(name) {
        currentConversation = name;
        api.messages(name).then(r => renderMessages(r.message.result || []));
        api.context(name).then(r => renderContext(r.message.result || {}));
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

    $('#wa-send-btn').on('click', function() {
        const body = $('#wa-composer-body').val();
        if (!currentConversation || !body) return;
        api.sendReply(currentConversation, body).then(() => {
            $('#wa-composer-body').val('');
            loadConversation(currentConversation);
            frappe.show_alert({message: 'Reply queued', indicator: 'green'});
        });
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

    api.conversations().then(r => renderConversations(r.message.result || []));
};
