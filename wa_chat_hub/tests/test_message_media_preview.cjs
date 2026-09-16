const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../wa_chat_hub/page/wa_chat_hub/wa_chat_hub.js'), 'utf8');
const context = {
    escapeHtml: value => String(value).replace(/&/g, '&amp;').replace(/"/g, '&quot;'),
    formatMessageTime: () => '12:00',
};
vm.createContext(context);
for (const name of ['safeMediaUrl', 'mediaFallbackHtml', 'renderMessageContent', 'isGenericCaption', 'parseJson']) {
    const start = source.indexOf(`    function ${name}(`);
    const end = source.indexOf('\n    function ', start + 1);
    assert.ok(start >= 0 && end > start);
    vm.runInContext(source.slice(start, end), context);
}
const render = context.renderMessageContent;
const local = render({ name: 'PHOTO', content_type: 'Image', media_url: '/files/photo.png', media_proxy_url: '/stale-proxy' });
assert.match(local, /class="wa-media-image"/);
assert.match(local, /src="\/files\/photo.png"/);
assert.ok(!local.includes('/stale-proxy'));
const template = render({ name: 'TEMPLATE', content_type: 'Template', media_content_type: 'Image', media_url: 'https://example.com/header.png', body: 'Hello patient' });
assert.match(template, /class="wa-media-image"/);
assert.match(template, /get_message_media\?message=TEMPLATE/);
assert.match(template, /wa-media-caption">Hello patient/);
const realtime = render({ name: 'LIVE', content_type: 'Template', media_url: 'https://example.com/media/123', raw_transport_payload: { header_format: 'IMAGE' } });
assert.match(realtime, /class="wa-media-image"/);
assert.match(realtime, /get_message_media\?message=LIVE/);
const incoming = render({ name: 'INBOUND', content_type: 'Image', media_url: 'https://example.com/a.png', media_proxy_url: '/existing-proxy' });
assert.match(incoming, /src="\/existing-proxy"/);
assert.equal(render({ content_type: 'Template', body: 'Text template' }), '<div class="wa-message-body">Text template</div>');
console.log('Media rendering passed: local uploads, image templates, realtime templates, incoming images, text templates.');
