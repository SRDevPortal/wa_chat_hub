import unittest
from unittest.mock import patch
import frappe
from wa_chat_hub import number_privacy as privacy

class NumberPrivacyTests(unittest.TestCase):
    def test_nested_sidebar_removes_phone_identity_without_changing_source(self):
        raw = {"contact": {"name": "9876501234", "phone_number": "9876501234", "display_name": "9876501234"},
               "conversation": {"name": "1042", "contact": "9876501234", "unread_count": 3},
               "raw_payload": {"secret": "9876501234"}}
        result = privacy.project(raw)
        self.assertNotIn('9876501234', str(result))
        self.assertEqual(result['conversation']['name'], '1042')
        self.assertEqual(result['conversation']['unread_count'], 3)
        self.assertEqual(raw['contact']['name'], '9876501234')

    def test_cache_hit_rechecks_policy(self):
        raw = {'phone_number': '9876501234'}
        fn = privacy.browser_response(lambda: raw)
        with patch.object(privacy, 'restricted', return_value=False):
            self.assertIs(fn(), raw)
        with patch.object(privacy, 'restricted', return_value=True):
            self.assertNotEqual(fn()['phone_number'], raw['phone_number'])
        self.assertEqual(raw['phone_number'], '9876501234')

    def test_message_payload_removed_after_media_extraction(self):
        from wa_chat_hub.api.chat import _attach_message_media_proxy_urls
        raw = {'name': '5821', 'content_type': 'Template', 'body': 'Call 9876501234',
               'raw_transport_payload': {'header_format': 'IMAGE', 'header_media_url': 'https://example.invalid/a.png',
                                         'payload': {'phone': '9876501234'}}}
        _attach_message_media_proxy_urls([raw])
        result = privacy.project(raw)
        self.assertNotIn('raw_transport_payload', result)
        self.assertEqual(result['media_content_type'], 'Image')
        self.assertTrue(result['media_proxy_url'].endswith('message=5821'))
        self.assertNotIn('9876501234', str(result))

    def test_realtime_notice_preserves_refresh_and_notification_metadata(self):
        from unittest.mock import Mock
        message = Mock(name='message')
        message.name = '5821'; message.direction = 'Inbound'; message.sender_type = 'Customer'
        with patch.object(privacy, 'enabled', return_value=True):
            result = privacy.message_event('1042', message)
        self.assertEqual(result['message_id'], '5821')
        self.assertTrue(result['refresh_required'])
        self.assertEqual(result['direction'], 'Inbound')
        message.as_dict.assert_not_called()
        with patch.object(privacy, 'enabled', return_value=False):
            self.assertEqual(privacy.message_event('1042', message)['message'], message.as_dict.return_value)

    def test_template_server_resolves_real_number_preserving_literal_values(self):
        convo = frappe._dict(contact='stored-contact', department='Support')
        with patch.object(privacy.frappe, 'get_doc', return_value=frappe._dict(phone_number='9876501234')) as get:
            result = privacy.resolve_template_values([{'field': 'contact.phone_number'}, 'literal', {'field': 'conversation.department'}], convo)
        self.assertEqual(result, ['9876501234', 'literal', 'Support'])
        get.assert_called_once_with('Chat Contact', 'stored-contact')

    def test_template_rejects_arbitrary_field_access(self):
        for field in ('contact.name', 'contact.password', 'other.phone_number'):
            with self.assertRaises(frappe.ValidationError):
                privacy.resolve_template_values([{'field': field}], frappe._dict())

    def test_message_authorization_precedes_projection(self):
        from wa_chat_hub.api import chat
        with patch.object(chat, 'ensure_can_read_conversation', side_effect=frappe.PermissionError), patch.object(chat.frappe, 'get_all') as query:
            with self.assertRaises(frappe.PermissionError):
                chat.get_messages.__wrapped__('1042')
            query.assert_not_called()
