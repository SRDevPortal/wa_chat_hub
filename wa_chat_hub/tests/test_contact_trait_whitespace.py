from copy import deepcopy
from unittest import TestCase
from unittest.mock import MagicMock, patch
from wa_chat_hub.interakt import contacts_api as api


class ContactTraitWhitespaceTests(TestCase):
    def test_request_normalizes_traits_without_mutating_erp_data(self):
        payload = {"phoneNumber": "9999999999", "countryCode": "+91",
                   "traits": {"name": " A\tB\nC   D ", "amount": 500, "enabled": False,
                              "empty": None, "normal": "A B"}, "tags": ["A B"]}
        original = deepcopy(payload)
        response = MagicMock(ok=True, content=b"{}")
        response.json.return_value = {"result": True}
        with patch.object(api, "get_interakt_account"), patch.object(api, "get_interakt_api_key",
                return_value="test"), patch.object(api, "track_users_api_url",
                return_value="https://test.invalid"), patch.object(api.requests, "post",
                return_value=response) as post:
            self.assertEqual(api.track_user("TEST", payload), {"result": True})
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["traits"], {"name": "A B C D", "amount": 500,
                                        "enabled": False, "empty": None, "normal": "A B"})
        self.assertEqual(payload, original)
        self.assertEqual(sent["phoneNumber"], original["phoneNumber"])
        self.assertEqual(sent["tags"], original["tags"])
