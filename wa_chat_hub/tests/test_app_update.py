from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from wa_chat_hub import app_update


class TestAppUpdateExecution(unittest.TestCase):
    def setUp(self):
        self.update = SimpleNamespace(name="Test Update", update_version="1.0")

    @patch("wa_chat_hub.app_update.frappe.get_single")
    @patch("wa_chat_hub.app_update.frappe.get_app_path")
    def test_script_receives_context_and_returns_result(self, get_app_path, get_single):
        get_app_path.return_value = "/tmp/bench/apps/wa_chat_hub/wa_chat_hub"
        get_single.return_value = MagicMock()

        result = app_update.execute_python_script(
            "print(update.name)\nresult = {'status': 'applied'}",
            self.update,
        )

        self.assertIn("Test Update", result)
        self.assertIn('"status": "applied"', result)

    @patch("wa_chat_hub.app_update.frappe.get_single")
    @patch("wa_chat_hub.app_update.frappe.get_app_path")
    def test_script_errors_are_raised(self, get_app_path, get_single):
        get_app_path.return_value = "/tmp/bench/apps/wa_chat_hub/wa_chat_hub"
        get_single.return_value = MagicMock()

        with self.assertRaisesRegex(RuntimeError, "expected failure"):
            app_update.execute_python_script("raise RuntimeError('expected failure')", self.update)

    def test_result_is_bounded(self):
        value = "x" * (app_update.MAX_RESULT_LENGTH + 100)
        bounded = app_update._bounded(value)

        self.assertLessEqual(len(bounded), app_update.MAX_RESULT_LENGTH + 30)
        self.assertTrue(bounded.endswith("...[output truncated]"))


if __name__ == "__main__":
    unittest.main()
