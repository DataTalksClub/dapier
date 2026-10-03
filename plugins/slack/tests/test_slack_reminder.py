"""slack_add_reminder: the reminders.add action (Zapier's "Add Reminder").

Unit tests drive the registered run callable with a fake transport; the
credential lookup is patched, matching the sibling Slack tests
(test_slack_actions.py, test_find_slack_dropbox.py).
"""
import json
import unittest
from unittest.mock import patch

import plugins.slack.plugin as slack_connector  # noqa: F401 (registers)
from src.dapier.connectors import registry
from plugins.slack.runners.slack import run_slack_add_reminder


class FakeTransport:
    """Serves canned JSON payloads in call order and records every request."""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "body": body})
        if len(self.calls) > len(self.payloads):
            raise AssertionError(f"unexpected extra call: {method} {url}")
        return 200, json.dumps(self.payloads[len(self.calls) - 1]).encode()


def run_reminder(transport, action, event=None):
    action = {"type": "slack_add_reminder", "credential_id": "slack",
              "text": "Rotate the {customer} key", **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value={"token": "xoxb-test"}):
        return run_slack_add_reminder(
            action, event or {"data": {"customer": "Acme"}}, transport=transport)


class SlackAddReminderTests(unittest.TestCase):
    def test_renders_text_and_posts_to_reminders_add(self):
        transport = FakeTransport({"ok": True, "reminder": {
            "id": "Rm08ABC123", "time": 1759400000,
            "text": "Rotate the Acme key"}})
        output = run_reminder(transport, {})

        self.assertTrue(output["ok"])
        self.assertEqual(output["reminder"]["id"], "Rm08ABC123")
        self.assertEqual(output["reminder"]["text"], "Rotate the Acme key")
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://slack.com/api/reminders.add")
        payload = json.loads(call["body"])
        self.assertEqual(payload, {"text": "Rotate the Acme key"})
        self.assertEqual(call["headers"]["authorization"], "Bearer xoxb-test")

    def test_natural_language_time_passes_through_as_a_string(self):
        transport = FakeTransport({"ok": True, "reminder": {
            "id": "Rm1", "time": "tomorrow 9am", "text": "standup"}})
        run_reminder(transport, {"text": "standup", "time": "tomorrow 9am"})

        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(payload, {"text": "standup", "time": "tomorrow 9am"})

    def test_bare_epoch_time_is_sent_as_a_number(self):
        transport = FakeTransport({"ok": True, "reminder": {
            "id": "Rm1", "time": 1759400000, "text": "deploy"}})
        run_reminder(transport, {"text": "deploy", "time": "1759400000"})

        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(payload["time"], 1759400000)

    def test_slack_error_raises_with_the_code(self):
        with self.assertRaisesRegex(RuntimeError, "time_in_past"):
            run_reminder(FakeTransport({"ok": False, "error": "time_in_past"}), {})

    def test_missing_text_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "requires rendered text"):
            run_reminder(FakeTransport(), {"text": " "})
        with self.assertRaisesRegex(ValueError, "requires rendered text"):
            run_reminder(FakeTransport(), {"text": "{missing_field}"})

    def test_action_is_registered_with_its_fields(self):
        self.assertIn("slack_add_reminder", registry.ACTIONS)
        required, optional = registry.action_specs()["slack_add_reminder"]
        self.assertEqual(required, frozenset({"text"}))
        self.assertEqual(optional,
                         frozenset({"time", "connection_id", "credential_id"}))
        entry = registry.ACTIONS["slack_add_reminder"]
        self.assertEqual([field["key"] for field in entry.fields],
                         ["connection_id", "text", "time", "credential_id"])


if __name__ == "__main__":
    unittest.main()
