frappe.pages['wa-chat-hub-errors'].on_page_load = function(wrapper) {
    const page = frappe.ui.make_app_page({
        parent: wrapper,
        title: 'WA Chat Hub Errors',
        single_column: true
    });

    const state = {
        since: '24h',
        category: 'all',
        search: '',
        events: [],
        categories: []
    };

    $(page.body).html(`
        <div class="wa-errors-page">
            <div class="wa-errors-toolbar">
                <select class="form-control input-sm" data-field="since">
                    <option value="1h">Last 1 hour</option>
                    <option value="24h" selected>Last 24 hours</option>
                    <option value="today">Today</option>
                    <option value="7d">Last 7 days</option>
                </select>
                <select class="form-control input-sm" data-field="category">
                    <option value="all">All types</option>
                </select>
                <input class="form-control input-sm" data-field="search" placeholder="Search phone, conversation, error" />
                <button class="btn btn-primary btn-sm" data-action="refresh">
                    <i class="fa fa-refresh"></i>
                    Refresh
                </button>
            </div>
            <div class="wa-errors-summary"></div>
            <div class="wa-errors-table-wrap">
                <table class="table table-bordered table-hover wa-errors-table">
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Type</th>
                            <th>Method</th>
                            <th>Reason</th>
                            <th>Phone</th>
                            <th>Conversation</th>
                        </tr>
                    </thead>
                    <tbody></tbody>
                </table>
            </div>
        </div>
    `);

    const $body = $(page.body);

    $body.on('change', '[data-field="since"], [data-field="category"]', function() {
        state[$(this).data('field')] = $(this).val();
        loadErrors();
    });

    $body.on('input', '[data-field="search"]', frappe.utils.debounce(function() {
        state.search = $(this).val() || '';
        loadErrors();
    }, 250));

    $body.on('click', '[data-action="refresh"]', function() {
        loadErrors();
    });

    $body.on('click', 'tbody tr[data-error-log]', function() {
        showDetail($(this).data('error-log'));
    });

    function loadErrors() {
        frappe.call({
            method: 'wa_chat_hub.api.errors.get_error_logs',
            args: {
                since: state.since,
                category: state.category,
                search: state.search,
                limit: 200
            },
            freeze: true,
            freeze_message: 'Loading WA Chat Hub errors...'
        }).then((r) => {
            const result = r.message && r.message.result ? r.message.result : {};
            state.events = result.events || [];
            state.categories = result.categories || [];
            renderCategoryOptions();
            renderSummary(result.summary || {});
            renderTable();
        });
    }

    function renderCategoryOptions() {
        const $select = $body.find('[data-field="category"]');
        const current = $select.val() || state.category;
        const options = ['<option value="all">All types</option>'].concat(
            state.categories.map((row) => `<option value="${frappe.utils.escape_html(row.value)}">${frappe.utils.escape_html(row.label)}</option>`)
        );
        $select.html(options.join(''));
        $select.val(current);
    }

    function renderSummary(summary) {
        const total = summary.total || 0;
        const counts = summary.by_category || {};
        const chips = state.categories.map((row) => {
            const count = counts[row.value] || 0;
            return `<span class="wa-error-chip">${frappe.utils.escape_html(row.label)} <b>${count}</b></span>`;
        });
        $body.find('.wa-errors-summary').html(`
            <div class="wa-errors-total">
                <span>Total</span>
                <strong>${total}</strong>
            </div>
            <div class="wa-errors-chips">${chips.join('')}</div>
        `);
    }

    function renderTable() {
        const rows = state.events.map((event) => `
            <tr data-error-log="${frappe.utils.escape_html(event.name)}">
                <td>${frappe.datetime.str_to_user(event.creation)}</td>
                <td><span class="wa-error-type">${frappe.utils.escape_html(event.category_label || '')}</span></td>
                <td>${frappe.utils.escape_html(event.method || '')}</td>
                <td>${frappe.utils.escape_html(event.short_reason || '')}</td>
                <td>${frappe.utils.escape_html(event.phone || '')}</td>
                <td>${event.conversation ? `<a href="/app/chat-conversation/${frappe.utils.escape_html(event.conversation)}">${frappe.utils.escape_html(event.conversation)}</a>` : ''}</td>
            </tr>
        `);
        $body.find('tbody').html(rows.join('') || `
            <tr>
                <td colspan="6" class="text-muted text-center">No WA Chat Hub errors found for this filter.</td>
            </tr>
        `);
    }

    function showDetail(errorLog) {
        frappe.call('wa_chat_hub.api.errors.get_error_detail', {
            error_log: errorLog
        }).then((r) => {
            const event = r.message.result;
            const related = event.related || {};
            const links = [];
            if (related.conversation) {
                links.push(`<a href="/app/chat-conversation/${frappe.utils.escape_html(related.conversation)}">Conversation ${frappe.utils.escape_html(related.conversation)}</a>`);
            }
            if (related.contact) {
                links.push(`<a href="/app/chat-contact/${frappe.utils.escape_html(related.contact)}">Contact ${frappe.utils.escape_html(related.contact)}</a>`);
            }
            if (related.crm_lead) {
                links.push(`<a href="/app/crm-lead/${frappe.utils.escape_html(related.crm_lead)}">CRM Lead ${frappe.utils.escape_html(related.crm_lead)}</a>`);
            }
            const contextHtml = event.wa_context
                ? `<div><b>WA Context:</b></div><pre>${frappe.utils.escape_html(JSON.stringify(event.wa_context, null, 2))}</pre>`
                : '';
            const payloadHtml = event.crm_lead_payload
                ? `<div><b>CRM Lead Payload:</b></div><pre>${frappe.utils.escape_html(JSON.stringify(event.crm_lead_payload, null, 2))}</pre>`
                : '';

            const dialog = new frappe.ui.Dialog({
                title: event.method || 'WA Chat Hub Error',
                size: 'extra-large',
                fields: [
                    {
                        fieldtype: 'HTML',
                        fieldname: 'detail',
                        options: `
                            <div class="wa-error-detail">
                                <div><b>Type:</b> ${frappe.utils.escape_html(event.category_label || '')}</div>
                                <div><b>Time:</b> ${frappe.datetime.str_to_user(event.creation)}</div>
                                <div><b>Reason:</b> ${frappe.utils.escape_html(event.short_reason || '')}</div>
                                <div><b>Suggested fix:</b> ${frappe.utils.escape_html(event.suggestion || '')}</div>
                                <div><b>Related:</b> ${links.join(' &middot; ') || '<span class="text-muted">No related records detected</span>'}</div>
                                ${payloadHtml}
                                ${contextHtml}
                                <pre>${frappe.utils.escape_html(event.error || '')}</pre>
                            </div>
                        `
                    }
                ]
            });
            dialog.show();
        });
    }

    loadErrors();
};
