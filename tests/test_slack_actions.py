"""slack_update_message / slack_add_reaction: message-maintenance actions
over the Slack Web API (Zapier's "Update Message" / "Add Reaction").

Unit tests drive the registered run callables with a fake transport; the
credential lookup is patched, matching the sibling find tests
(test_find_slack_dropbox.py, test_dropbox_slack_actions.py).
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import slack as slack_connector  # noqa: F401 (registers)
from src.dapier.connectors import registry
from src.dapier.engine.actions.slack import run_slack_add_reaction, run_slack_update_message


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


def run_update(transport, action, event=None):
    action = {"type": "slack_update_message", "credential_id": "slack",
              "channel": "{channel_id}", "ts": "{ts}", "text": "{text} (edited)",
              **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value={"token": "xoxb-test"}):
        return run_slack_update_message(
            action, event or {"data": {"channel_id": "C1", "ts": "111.222",
                                       "text": "ship it"}},
            transport=transport)


def run_reaction(transport, action, event=None):
    action = {"type": "slack_add_reaction", "credential_id": "slack",
              "channel": "{channel_id}", "timestamp": "{ts}",
              "reaction": "tada", **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value={"token": "xoxb-test"}):
        return run_slack_add_reaction(
            action, event or {"data": {"channel_id": "C1", "ts": "111.222"}},
            transport=transport)


class SlackUpdateMessageTests(unittest.TestCase):
    def test_renders_channel_ts_and_text_into_chat_update(self):
        transport = FakeTransport({"ok": True, "channel": "C1", "ts": "111.222"})
        output = run_update(transport, {})

        self.assertTrue(output["ok"])
        self.assertEqual(output["ts"], "111.222")
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://slack.com/api/chat.update")
        payload = json.loads(call["body"])
        self.assertEqual(payload["channel"], "C1")
        self.assertEqual(payload["ts"], "111.222")
        self.assertEqual(payload["text"], "ship it (edited)")
        self.assertEqual(call["headers"]["authorization"], "Bearer xoxb-test")

    def test_slack_error_raises_with_the_code(self):
        with self.assertRaisesRegex(RuntimeError, "message_not_found"):
            run_update(FakeTransport({"ok": False, "error": "message_not_found"}), {})

    def test_missing_channel_or_ts_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "channel and ts"):
            run_update(FakeTransport(), {"channel": "", "ts": "{ts}"})
        with self.assertRaisesRegex(ValueError, "channel and ts"):
            run_update(FakeTransport(), {"channel": "{channel_id}", "ts": " "})

    def test_action_is_registered_with_its_fields(self):
        self.assertIn("slack_update_message", registry.ACTIONS)
        required, _optional = registry.action_specs()["slack_update_message"]
        self.assertEqual(required, frozenset({"channel", "ts", "text"}))


class SlackAddReactionTests(unittest.TestCase):
    def test_reacts_to_the_rendered_message(self):
        transport = FakeTransport({"ok": True})
        output = run_reaction(transport, {})

        self.assertTrue(output["ok"])
        self.assertEqual(output["reaction"], "tada")
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://slack.com/api/reactions.add")
        payload = json.loads(call["body"])
        self.assertEqual(payload, {"channel": "C1", "timestamp": "111.222",
                                   "name": "tada"})

    def test_strips_colons_from_the_emoji_name(self):
        transport = FakeTransport({"ok": True})
        output = run_reaction(transport, {"reaction": ":tada:"})
        payload = json.loads(transport.calls[0]["body"])
        self.assertEqual(payload["name"], "tada")
        self.assertEqual(output["reaction"], "tada")

    def test_already_reacted_is_a_success(self):
        transport = FakeTransport({"ok": False, "error": "already_reacted"})
        output = run_reaction(transport, {})

        self.assertTrue(output["ok"])
        self.assertTrue(output["already_reacted"])

    def test_other_slack_errors_raise(self):
        with self.assertRaisesRegex(RuntimeError, "no_item"):
            run_reaction(FakeTransport({"ok": False, "error": "no_item"}), {})

    def test_missing_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "reaction"):
            run_reaction(FakeTransport(), {"reaction": ""})

    def test_action_is_registered_with_its_fields(self):
        self.assertIn("slack_add_reaction", registry.ACTIONS)
        required, _optional = registry.action_specs()["slack_add_reaction"]
        self.assertEqual(required, frozenset({"channel", "timestamp", "reaction"}))


if __name__ == "__main__":
    unittest.main()
