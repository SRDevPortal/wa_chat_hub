const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function setup(result) {
    const calls = [], routes = [], messages = [], handlers = {};
    const context = { __: text => text, wa_chat_hub: {}, frappe: {
        provide() {},
        call: async args => { calls.push(args); return { message: result }; },
        set_route: route => routes.push(route),
        msgprint: message => messages.push(message),
        ui: { form: { on: (dt, handler) => { handlers[dt] = handler; } },
            Dialog: function(options) { context.dialog = options; this.show = () => {}; this.hide = () => {}; } },
    }};
    vm.createContext(context);
    for (const file of ['reference_chat.js', 'reference_chat_form.js']) {
        vm.runInContext(fs.readFileSync(path.join(__dirname, '../public/js', file), 'utf8'), context);
    }
    return { context, calls, routes, messages, handlers };
}

test('Lead and Customer form refresh makes no lookup; saved record button opens exact thread', async () => {
    const s = setup({ conversations: [{ name: '123' }] });
    for (const doctype of ['Lead', 'Customer']) {
        let button;
        s.handlers[doctype].refresh({ is_new: () => false, doctype, doc: { name: 'record' },
            add_custom_button: (label, callback) => { assert.equal(label, 'Open WhatsApp Chat'); button = callback; } });
        const before = s.calls.length;
        assert.equal(s.calls.length, before);
        button();
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(s.calls.length, before + 1);
        assert.equal(s.context.frappe.route_options.conversation, '123');
        assert.equal(s.routes.at(-1), 'wa-chat-hub');
    }
    s.handlers.Lead.refresh({ is_new: () => true, add_custom_button: () => assert.fail('unsaved button') });
});

test('multiple matches require selection, no automatic first-thread redirect', async () => {
    const s = setup({ conversations: [{ name: '1', channel_account: 'A', status: 'Open' },
        { name: '2', channel_account: 'B', status: 'Closed' }] });
    await s.context.wa_chat_hub.open_reference_chat('Customer', 'record');
    assert.equal(s.routes.length, 0);
    s.context.dialog.primary_action({ conversation: s.context.dialog.fields[0].options[1] });
    assert.equal(s.context.frappe.route_options.conversation, '2');
});

test('no match stays on record and repeated clicks coalesce to one lookup', async () => {
    const s = setup({ conversations: [], message: 'No conversation found' });
    await Promise.all([s.context.wa_chat_hub.open_reference_chat('Lead', 'record'),
        s.context.wa_chat_hub.open_reference_chat('Lead', 'record')]);
    assert.equal(s.calls.length, 1);
    assert.equal(s.routes.length, 0);
    assert.equal(s.messages[0].message, 'No conversation found');
});
