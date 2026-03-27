frappe.pages['wa-chat-hub-settings'].on_page_load = function(wrapper) {
    frappe.ui.make_app_page({
        parent: wrapper,
        title: 'WA Chat Hub Settings',
        single_column: true
    });

    $(wrapper).html(`
        <div class="wa-settings-layout">
            <div class="wa-settings-card">
                <h3>AI Control Plane</h3>
                <p>Model behavior, autopilot mode, guardrails, ERP access, and tool permissions.</p>
                <ul>
                    <li>Enable AI drafts / summaries</li>
                    <li>Autopilot mode</li>
                    <li>Allowed ERP actions</li>
                    <li>MCP/tool access policy</li>
                </ul>
            </div>
            <div class="wa-settings-card">
                <h3>Knowledge Base</h3>
                <p>Add SOPs, FAQs, treatment-safe response guidance, department playbooks, and external references.</p>
            </div>
            <div class="wa-settings-card">
                <h3>MCP Servers</h3>
                <p>Register MCP servers, transport, auth, scopes, and allowed tools for AI/agent operations.</p>
            </div>
        </div>
    `);
};
