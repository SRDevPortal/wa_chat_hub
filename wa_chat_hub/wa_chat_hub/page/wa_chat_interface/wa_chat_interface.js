frappe.pages['wa-chat-interface'].on_page_load = function(wrapper) {
	var page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'WA Chat Hub',
		single_column: true
	});

	page.main.html(`
		<div id="wa-chat-app" class="wa-chat-app">
			<div class="chat-sidebar">
				<div class="chat-list" id="chat-list"></div>
			</div>
			<div class="chat-main">
				<div class="chat-area" style="display:none;" id="chat-area">
					<div class="chat-header">
						<h4 id="active-chat-header">Contact</h4>
					</div>
					<div class="chat-messages" id="chat-messages">
					</div>
					<div class="chat-input-area">
						<input type="text" class="form-control" id="chat-input" placeholder="Type a message...">
						<button class="btn btn-primary" style="margin-left: 10px;" id="send-btn">
							<i class="fa fa-paper-plane"></i> Send
						</button>
					</div>
				</div>
                <div class="chat-placeholder" id="chat-placeholder">
					<div class="text-center text-muted">
						<i class="fa fa-comments fa-3x mb-3"></i>
						<h4>Select a conversation to start chatting.</h4>
					</div>
				</div>
			</div>
		</div>
	`);

	page.add_inner_button('<i class="fa fa-list"></i> List View', function() {
		frappe.set_route('List', 'Chat Conversation');
	});

    let all_conversations = [];
    let active_conversation = null;

    function render_conversations(conversations) {
        let html = '';
        if(conversations.length === 0) {
            html = '<div class="text-muted p-4 text-center">No conversations found.</div>';
        } else {
            conversations.forEach(conv => {
                let nameStr = conv.display_name || conv.phone_number || conv.name;
                let activeCls = (active_conversation === conv.name) ? 'active' : '';
                html += `
                    <div class="chat-list-item ${activeCls}" data-id="${conv.name}" data-name="${nameStr}">
						<div class="chat-avatar">${nameStr.substring(0, 1)}</div>
						<div class="chat-summary">
							<strong>${nameStr}</strong>
						</div>
					</div>
                `;
            });
        }
        $('#chat-list').html(html);

        $('.chat-list-item').on('click', function() {
            $('.chat-list-item').removeClass('active');
            $(this).addClass('active');
            active_conversation = $(this).attr('data-id');
            $('#chat-placeholder').hide();
            $('#chat-area').show();
            $('#active-chat-header').text($(this).attr('data-name'));
            fetch_messages();
        });
    }

    function render_messages(messages) {
        let html = '';
        messages.forEach(msg => {
            let dirCls = msg.direction.toLowerCase();
            html += `
                <div class="message ${dirCls}">
					<div class="message-bubble">
						${msg.body}
						<div class="message-meta">${frappe.datetime.global_date_format(msg.creation || frappe.datetime.now_datetime())}</div>
					</div>
				</div>
            `;
        });
        $('#chat-messages').html(html);
        let el = document.getElementById('chat-messages');
		if (el) el.scrollTop = el.scrollHeight;
    }

	function fetch_conversations() {
		frappe.call({
			method: 'wa_chat_hub.wa_chat_hub.page.wa_chat_interface.wa_chat_interface.get_conversations',
			callback: function(r) {
				if(r.message) {
					all_conversations = r.message;
                    render_conversations(all_conversations);
                    
                    if (frappe.route_options && frappe.route_options.conversation) {
                        let target = frappe.route_options.conversation;
                        frappe.route_options = null;

                        setTimeout(() => {
                            $(`.chat-list-item[data-id="${target}"]`).click();
                        }, 100);
                    }
                }
			}
		});
	}

    function fetch_messages() {
        if(!active_conversation) return;
        frappe.call({
			method: 'wa_chat_hub.wa_chat_hub.page.wa_chat_interface.wa_chat_interface.get_messages',
			args: { conversation: active_conversation },
			callback: function(r) {
				if (r.message) render_messages(r.message);
			}
		});
    }

    function send_message() {
        let body = $('#chat-input').val();
        if(!body || !active_conversation) return;
        $('#chat-input').val('');
        
        frappe.call({
			method: 'wa_chat_hub.wa_chat_hub.page.wa_chat_interface.wa_chat_interface.send_message',
			args: { conversation: active_conversation, body: body }
		});
    }

    $('#send-btn').on('click', send_message);
    $('#chat-input').on('keypress', function(e) {
        if(e.which === 13) send_message();
    });

	fetch_conversations();
	
	frappe.realtime.on("wa_chat_new_message", function(data) {
		if (active_conversation === data.conversation) {
			fetch_messages();
		}
		fetch_conversations();
	});
}
