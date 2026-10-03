"""The dropbox create-folder/move/copy and slack DM/create-channel/
set-topic/set-purpose actions: request shapes, rendered paths, and output
envelopes — Zapier's file-management and channel-management actions over
Dropbox's files RPC and Slack's conversations/reactions Web API.

Unit tests drive the registered run callables with a fake transport; the
connection/credential lookups are patched, matching the sibling action
tests (test_find_slack_dropbox.py, test_action_breadth.py).
"""
import json
import unittest
from unittest.mock import patch

import plugins.dropbox.plugin as dropbox_connector  # noqa: F401 (registers)
import plugins.slack.plugin as slack_connector  # noqa: F401 (registers)
from src.dapier.connectors import registry
from plugins.dropbox.runners.dropbox import (
    run_dropbox_copy,
    run_dropbox_create_folder,
    run_dropbox_move,
)
from plugins.slack.runners.slack import (
    run_slack_create_channel,
    run_slack_dm,
    run_slack_set_purpose,
    run_slack_set_topic,
)


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


class StatusTransport(FakeTransport):
    """One canned response served with an explicit HTTP status."""

    def __init__(self, status, payload):
        super().__init__(payload)
        self.status = status

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        status = self.status
        result = super().__call__(method, url, headers=headers, body=body,
                                  timeout=timeout)
        return status, result[1]


DROPBOX_CONNECTION = {"connection_id": "dbx", "provider": "dropbox",
                      "status": "connected", "credential_id": "oauth#dbx",
                      "root_path": "/team/shared"}


def run_dropbox(runner, transport, action, event=None):
    action = {"connection_id": "dbx", **action}
    with patch("plugins.dropbox.runners.dropbox._dropbox_connection",
               return_value=DROPBOX_CONNECTION), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return runner(action, event or {"data": {}}, transport=transport)


class DropboxCreateFolderTests(unittest.TestCase):
    def test_creates_a_folder_and_returns_its_metadata(self):
        transport = FakeTransport({"metadata": {
            "id": "id:new", "name": "2026-09", ".tag": "folder",
            "path_display": "/team/shared/Invoices/2026-09"}})
        output = run_dropbox(run_dropbox_create_folder, transport,
                             {"path": "/Invoices/{month}"},
                             event={"data": {"month": "2026-09"}})

        self.assertEqual(output, {"folder": "/Invoices/2026-09", "item": {
            "id": "id:new", "name": "2026-09", "path": "/team/shared/Invoices/2026-09",
            "tag": "folder"}})
        call = transport.calls[0]
        self.assertEqual(call["url"],
                         "https://api.dropboxapi.com/2/files/create_folder_v2")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(json.loads(call["body"]),
                         {"path": "/Invoices/2026-09"})

    def test_existing_folder_conflict_raises_with_the_tag(self):
        transport = StatusTransport(409, {
            "error": {".tag": "path", "path": {".tag": "conflict"}}})
        with self.assertRaises(RuntimeError) as caught:
            run_dropbox(run_dropbox_create_folder, transport,
                        {"path": "/Invoices"})
        self.assertIn("HTTP 409", str(caught.exception))

    def test_missing_path_is_rejected(self):
        with self.assertRaises(ValueError):
            run_dropbox(run_dropbox_create_folder, FakeTransport(), {})


class DropboxMoveCopyTests(unittest.TestCase):
    def test_move_renders_both_paths_and_returns_metadata(self):
        transport = FakeTransport({"metadata": {
            "id": "id:f1", "name": "invoice.pdf", ".tag": "file",
            "path_display": "/team/shared/Archive/invoice.pdf",
            "size": 1234, "server_modified": "2026-09-28T10:00:00Z"}})
        output = run_dropbox(run_dropbox_move, transport,
                             {"from_path": "{path}",
                              "to_path": "/Archive/{filename}"},
                             event={"data": {"path": "/Invoices/invoice.pdf",
                                             "filename": "invoice.pdf"}})

        self.assertEqual(output["moved"], "/Archive/invoice.pdf")
        self.assertEqual(output["item"]["path"], "/team/shared/Archive/invoice.pdf")
        self.assertEqual(json.loads(transport.calls[0]["body"]), {
            "from_path": "/Invoices/invoice.pdf",
            "to_path": "/Archive/invoice.pdf",
            "autorename": False})
        self.assertEqual(transport.calls[0]["url"],
                         "https://api.dropboxapi.com/2/files/move_v2")

    def test_move_autorename_is_passed_through(self):
        transport = FakeTransport({"metadata": {
            "id": "id:f1", "name": "invoice (1).pdf", ".tag": "file",
            "path_display": "/team/shared/Archive/invoice (1).pdf"}})
        run_dropbox(run_dropbox_move, transport,
                    {"from_path": "/a.pdf", "to_path": "/b.pdf",
                     "autorename": True})

        self.assertTrue(json.loads(transport.calls[0]["body"])["autorename"])

    def test_copy_hits_copy_v2_and_reports_copied(self):
        transport = FakeTransport({"metadata": {
            "id": "id:f2", "name": "invoice.pdf", ".tag": "file",
            "path_display": "/team/shared/Keep/invoice.pdf"}})
        output = run_dropbox(run_dropbox_copy, transport,
                             {"from_path": "/Invoices/invoice.pdf",
                              "to_path": "/Keep/invoice.pdf"})

        self.assertEqual(output["copied"], "/Keep/invoice.pdf")
        self.assertEqual(transport.calls[0]["url"],
                         "https://api.dropboxapi.com/2/files/copy_v2")
        self.assertEqual(json.loads(transport.calls[0]["body"]), {
            "from_path": "/Invoices/invoice.pdf",
            "to_path": "/Keep/invoice.pdf",
            "autorename": False})

    def test_move_without_paths_is_rejected(self):
        with self.assertRaises(ValueError):
            run_dropbox(run_dropbox_move, FakeTransport(), {"from_path": "/a.pdf"})


SLACK_TOKEN = {"token": "xoxb-test"}


class SlackDmTests(unittest.TestCase):
    def run_dm(self, transport, action, event=None):
        action = {"credential_id": "slack", **action}
        with patch("src.dapier.connections.credentials.get_credential",
                   return_value=SLACK_TOKEN):
            return run_slack_dm(action, event or {"data": {}},
                                transport=transport)

    def test_opens_the_dm_channel_then_posts(self):
        transport = FakeTransport(
            {"ok": True, "channel": {"id": "D0DMCHANNEL", "user": "U1"}},
            {"ok": True, "channel": "D0DMCHANNEL", "ts": "1759000000.000100"})
        output = self.run_dm(transport, {"user_id": "{user}", "text": "hi {user}"},
                             event={"data": {"user": "U1"}})

        self.assertEqual(output, {"ok": True, "user": "U1",
                                  "channel": "D0DMCHANNEL",
                                  "ts": "1759000000.000100"})
        open_call, post_call = transport.calls
        self.assertEqual(open_call["url"],
                         "https://slack.com/api/conversations.open")
        self.assertEqual(json.loads(open_call["body"]),
                         {"users": "U1", "return_im": True})
        self.assertEqual(post_call["url"],
                         "https://slack.com/api/chat.postMessage")
        self.assertEqual(json.loads(post_call["body"])["channel"], "D0DMCHANNEL")

    def test_open_failure_raises_with_the_error_code(self):
        transport = FakeTransport({"ok": False, "error": "user_not_found"})
        with self.assertRaises(RuntimeError) as caught:
            self.run_dm(transport, {"user_id": "U404"})
        self.assertIn("user_not_found", str(caught.exception))

    def test_post_failure_raises(self):
        transport = FakeTransport(
            {"ok": True, "channel": {"id": "D1"}},
            {"ok": False, "error": "is_archived"})
        with self.assertRaises(RuntimeError):
            self.run_dm(transport, {"user_id": "U1", "text": "hi"})

    def test_missing_user_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_dm(FakeTransport(), {"user_id": ""})


class SlackCreateChannelTests(unittest.TestCase):
    def run_create(self, transport, action, event=None):
        action = {"credential_id": "slack", **action}
        with patch("src.dapier.connections.credentials.get_credential",
                   return_value=SLACK_TOKEN):
            return run_slack_create_channel(action, event or {"data": {}},
                                            transport=transport)

    def test_normalizes_the_name_and_creates_the_channel(self):
        transport = FakeTransport({"ok": True, "channel": {
            "id": "C0NEW", "name": "alerts-acme", "is_private": False}})
        output = self.run_create(transport, {"name": "Alerts {customer}"},
                                 event={"data": {"customer": "ACME"}})

        self.assertEqual(output, {"ok": True, "channel": {
            "id": "C0NEW", "name": "alerts-acme", "is_private": False}})
        call = transport.calls[0]
        self.assertEqual(call["url"],
                         "https://slack.com/api/conversations.create")
        self.assertEqual(json.loads(call["body"]),
                         {"name": "alerts-acme", "is_private": False})

    def test_is_private_passes_through(self):
        transport = FakeTransport({"ok": True, "channel": {
            "id": "C0PRIV", "name": "incidents", "is_private": True}})
        output = self.run_create(transport, {"name": "incidents",
                                             "is_private": True})

        self.assertTrue(output["channel"]["is_private"])
        self.assertTrue(json.loads(transport.calls[0]["body"])["is_private"])

    def test_name_taken_raises(self):
        transport = FakeTransport({"ok": False, "error": "name_taken"})
        with self.assertRaises(RuntimeError) as caught:
            self.run_create(transport, {"name": "general"})
        self.assertIn("name_taken", str(caught.exception))

    def test_a_name_that_normalizes_to_empty_is_rejected(self):
        with self.assertRaises(ValueError):
            self.run_create(FakeTransport(), {"name": "***"})


class SlackSetTopicPurposeTests(unittest.TestCase):
    def run_set(self, runner, transport, action, event=None):
        action = {"credential_id": "slack", **action}
        with patch("src.dapier.connections.credentials.get_credential",
                   return_value=SLACK_TOKEN):
            return runner(action, event or {"data": {}}, transport=transport)

    def test_set_topic_renders_channel_and_topic(self):
        transport = FakeTransport({"ok": True, "channel": "C01",
                                   "topic": "Deploy chatter"})
        output = self.run_set(run_slack_set_topic, transport,
                              {"channel": "{channel_id}", "topic": "Deploy chatter"},
                              event={"data": {"channel_id": "C01"}})

        self.assertEqual(output, {"ok": True, "channel": "C01",
                                  "topic": "Deploy chatter"})
        call = transport.calls[0]
        self.assertEqual(call["url"],
                         "https://slack.com/api/conversations.setTopic")
        self.assertEqual(json.loads(call["body"]),
                         {"channel": "C01", "topic": "Deploy chatter"})

    def test_set_purpose_hits_set_purpose(self):
        transport = FakeTransport({"ok": True, "channel": "C01",
                                   "purpose": "Incident coordination"})
        output = self.run_set(run_slack_set_purpose, transport,
                              {"channel": "C01", "purpose": "Incident coordination"})

        self.assertEqual(output["purpose"], "Incident coordination")
        self.assertEqual(transport.calls[0]["url"],
                         "https://slack.com/api/conversations.setPurpose")

    def test_slack_error_raises_with_the_code(self):
        transport = FakeTransport({"ok": False, "error": "not_in_channel"})
        with self.assertRaises(RuntimeError) as caught:
            self.run_set(run_slack_set_topic, transport,
                         {"channel": "C01", "topic": "x"})
        self.assertIn("not_in_channel", str(caught.exception))

    def test_missing_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            self.run_set(run_slack_set_topic, FakeTransport(),
                         {"channel": "C01", "topic": ""})
        with self.assertRaises(ValueError):
            self.run_set(run_slack_set_purpose, FakeTransport(),
                         {"channel": "", "purpose": "x"})


class RegistrationTests(unittest.TestCase):
    def test_dropbox_actions_are_registered(self):
        for action_type in ("dropbox_create_folder", "dropbox_move",
                            "dropbox_copy"):
            self.assertIn(action_type, registry.ACTIONS)

    def test_slack_actions_are_registered(self):
        for action_type in ("slack_dm", "slack_create_channel",
                            "slack_set_topic", "slack_set_purpose"):
            self.assertIn(action_type, registry.ACTIONS)

    def test_registered_specs_accept_their_own_fields(self):
        specs = registry.action_specs()
        required, optional = specs["dropbox_move"]
        self.assertIn("from_path", required)
        self.assertIn("autorename", optional)
        required, optional = specs["slack_dm"]
        self.assertIn("user_id", required)
        self.assertIn("text", optional)


if __name__ == "__main__":
    unittest.main()
