"""Backfill messaging window fields from Chat Message history."""

import frappe

from wa_chat_hub.messaging.windows import backfill_messaging_windows_from_history


def execute():
    backfill_messaging_windows_from_history()
