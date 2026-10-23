const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../wa_chat_hub/page/wa_chat_hub/wa_chat_hub.js'), 'utf8');
const start = source.indexOf('    function templateVariableSlots(');
const end = source.indexOf('    function templateHasBodyVariables(', start);
assert.ok(start >= 0 && end > start);
const context = vm.createContext({});
vm.runInContext(source.slice(start, end), context);
const slots = (row, section = 'body') => JSON.parse(JSON.stringify(context.templateVariableSlots(row, section)));

test('multilingual repeated parameters have one input each in numeric order', () => {
    const row = {body_preview: '{{2}} नमस्ते {{1}} hello {{2}} {{1}}', body_variable_count: 4};
    assert.deepEqual(slots(row).map(s => s.index), [1, 2]);
    assert.deepEqual(slots({body_preview: '{{1}} नमस्ते {{1}}'}).map(s => s.index), [1]);
});

test('API slots are deduplicated and sorted without mutating shared metadata', () => {
    const row = {body_variables: [{index: 2}, {index: 1}, {index: 2}]};
    assert.deepEqual(slots(row).map(s => s.index), [1, 2]);
    assert.deepEqual(row.body_variables.map(s => s.index), [2, 1, 2]);
});

test('missing preview retains count fallback and headers stay independent', () => {
    const row = {body_variable_count: 3, header_preview: '{{1}} {{1}}'};
    assert.deepEqual(slots(row).map(s => s.index), [1, 2, 3]);
    assert.deepEqual(slots(row, 'header').map(s => s.index), [1]);
    assert.deepEqual(slots({}), []);
});
