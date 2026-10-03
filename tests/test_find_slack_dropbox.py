"""slack_find / slack_find_user and dropbox_find: found/not-found outputs,
kind filters and create-if-missing — Zapier's find-record semantics over
Slack's Web API and Dropbox's files/search.

Unit tests drive the registered run callables with a fake transport; the
connection/credential lookups are patched, matching the sibling find tests
(test_find_s3_drive.py, test_sheets_find_row.py).
"""
import json
import unittest
from unittest.mock import patch

import plugins.dropbox.plugin as dropbox_connector  # noqa: F401 (registers)
import plugins.slack.plugin as slack_connector  # noqa: F401 (registers)
from src.dapier.connectors import registry
from plugins.dropbox.runners.dropbox import run_dropbox_find
from plugins.slack.runners.slack import run_slack_find, run_slack_find_user


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


DROPBOX_CONNECTION = {"connection_id": "dbx", "provider": "dropbox",
                      "status": "connected", "credential_id": "oauth#dbx",
                      "root_path": "/team/shared"}


def run_dropbox(transport, action, event=None):
    action = {"type": "dropbox_find", "connection_id": "dbx",
              "query": "report.pdf", **action}
    with patch("plugins.dropbox.runners.dropbox._dropbox_connection",
               return_value=DROPBOX_CONNECTION), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_dropbox_find(action, event or {"data": {}}, transport=transport)


def run_slack(transport, action, event=None):
    action = {"type": "slack_find", "credential_id": "slack",
              "find": "user", "query": "person@example.com", **action}
    with patch("src.dapier.connections.credentials.get_credential",
               return_value={"token": "xoxb-test"}):
        return run_slack_find(action, event or {"data": {}}, transport=transport)


class SlackFindTests(unittest.TestCase):
    def test_finds_a_user_by_email(self):
        transport = FakeTransport({"ok": True, "user": {
            "id": "U1", "name": "jane", "real_name": "Jane Doe",
            "tz": "Europe/Berlin",
            "profile": {"email": "person@example.com"}}})
        output = run_slack(transport, {})

        self.assertTrue(output["found"])
        self.assertEqual(output["user"]["id"], "U1")
        self.assertEqual(output["user"]["email"], "person@example.com")
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://slack.com/api/users.lookupByEmail")
        self.assertEqual(call["headers"]["authorization"], "Bearer xoxb-test")
        self.assertEqual(json.loads(call["body"]), {"email": "person@example.com"})

    def test_unknown_email_is_found_false_not_an_error(self):
        transport = FakeTransport({"ok": False, "error": "users_not_found"})
        output = run_slack(transport, {})

        self.assertEqual(output, {"found": False, "user": None})

    def test_other_slack_errors_raise(self):
        transport = FakeTransport({"ok": False, "error": "invalid_auth"})
        with self.assertRaises(RuntimeError):
            run_slack(transport, {})

    def test_channel_lookup_follows_cursor_pages(self):
        transport = FakeTransport(
            {"ok": True, "channels": [{"id": "C1", "name": "general"}],
             "response_metadata": {"next_cursor": "cur-2"}},
            {"ok": True, "channels": [{"id": "C2", "name": "The Docks"}]},
        )
        output = run_slack(transport, {"find": "channel", "query": "#the docks"})

        self.assertTrue(output["found"])
        self.assertEqual(output["channel"],
                         {"id": "C2", "name": "The Docks", "is_private": False})
        self.assertEqual(json.loads(transport.calls[1]["body"])["cursor"], "cur-2")
        self.assertEqual(len(transport.calls), 2)

    def test_channel_miss_is_found_false(self):
        transport = FakeTransport({"ok": True,
                                   "channels": [{"id": "C1", "name": "general"}]})
        output = run_slack(transport, {"find": "channel", "query": "missing"})

        self.assertEqual(output, {"found": False, "channel": None})
        self.assertEqual(len(transport.calls), 1)

    def test_query_renders_from_the_event(self):
        transport = FakeTransport({"ok": False, "error": "users_not_found"})
        output = run_slack(transport, {"query": "{email}"},
                           event={"data": {"email": "who@example.com"}})

        self.assertFalse(output["found"])
        self.assertEqual(json.loads(transport.calls[0]["body"]),
                         {"email": "who@example.com"})

    def test_unknown_find_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            run_slack(FakeTransport(), {"find": "group"})

    def test_single_purpose_user_find_renders_email(self):
        transport = FakeTransport({"ok": True, "user": {
            "id": "U9", "name": "z", "profile": {"email": "z@example.com"}}})
        with patch("src.dapier.connections.credentials.get_credential",
                   return_value={"token": "xoxb-test"}):
            output = run_slack_find_user(
                {"type": "slack_find_user", "credential_id": "slack",
                 "email": "{handle}"},
                {"data": {"handle": "z@example.com"}}, transport=transport)

        self.assertTrue(output["found"])
        self.assertEqual(output["user"]["id"], "U9")
        self.assertEqual(json.loads(transport.calls[0]["body"])["email"],
                         "z@example.com")


class DropboxFindTests(unittest.TestCase):
    def test_finds_a_file_under_the_connection_root(self):
        transport = FakeTransport({"matches": [{"metadata": {
            "id": "id:f1", "name": "report.pdf", ".tag": "file",
            "path_display": "/team/shared/report.pdf",
            "size": 1234, "server_modified": "2026-09-26T21:03:49Z"}}]})
        output = run_dropbox(transport, {})

        self.assertTrue(output["found"])
        self.assertFalse(output["created"])
        self.assertEqual(output["item"], {
            "id": "id:f1", "name": "report.pdf",
            "path": "/team/shared/report.pdf", "tag": "file",
            "size": 1234, "modified": "2026-09-26T21:03:49Z"})
        call = transport.calls[0]
        self.assertEqual(call["url"], "https://api.dropboxapi.com/2/files/search")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(json.loads(call["body"]), {
            "query": "report.pdf",
            "options": {"path": "/team/shared", "max_results": 50,
                        "file_status": "active", "filename_only": True}})

    def test_kind_filter_skips_the_other_kind(self):
        transport = FakeTransport({"matches": [
            {"metadata": {"id": "id:d1", "name": "Archive", ".tag": "folder",
                          "path_display": "/team/shared/Archive"}},
            {"metadata": {"id": "id:f1", "name": "report.pdf", ".tag": "file",
                          "path_display": "/team/shared/report.pdf"}},
        ]})
        output = run_dropbox(transport, {"kind": "file"})

        self.assertEqual(output["item"]["id"], "id:f1")

    def test_explicit_path_overrides_the_connection_root(self):
        transport = FakeTransport({"matches": []})
        run_dropbox(transport, {"path": "/Other"})

        self.assertEqual(json.loads(transport.calls[0]["body"])["options"]["path"],
                         "/Other")

    def test_query_renders_from_the_event(self):
        transport = FakeTransport({"matches": []})
        run_dropbox(transport, {"query": "{filename}"},
                    event={"data": {"filename": "invoice.pdf"}})

        self.assertEqual(json.loads(transport.calls[0]["body"])["query"],
                         "invoice.pdf")

    def test_miss_is_found_false_and_makes_one_call(self):
        transport = FakeTransport({"matches": []})
        output = run_dropbox(transport, {})

        self.assertEqual(output, {"found": False, "created": False, "item": None})
        self.assertEqual(len(transport.calls), 1)

    def test_create_if_missing_creates_the_folder(self):
        transport = FakeTransport(
            {"matches": []},
            {"metadata": {"id": "id:new", "name": "Invoices", ".tag": "folder",
                          "path_display": "/team/shared/Invoices"}})
        output = run_dropbox(transport, {"kind": "folder", "query": "Invoices",
                                         "create_if_missing": True})

        self.assertEqual(output, {"found": False, "created": True, "item": {
            "id": "id:new", "name": "Invoices",
            "path": "/team/shared/Invoices", "tag": "folder"}})
        self.assertEqual(transport.calls[1]["url"],
                         "https://api.dropboxapi.com/2/files/create_folder_v2")
        self.assertEqual(json.loads(transport.calls[1]["body"]),
                         {"path": "/team/shared/Invoices"})

    def test_create_if_missing_only_applies_to_folders(self):
        transport = FakeTransport({"matches": []})
        with self.assertRaises(ValueError):
            run_dropbox(transport, {"kind": "file", "create_if_missing": True})

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            run_dropbox(FakeTransport(), {"kind": "archive"})

    def test_missing_query_is_rejected(self):
        with self.assertRaises(ValueError):
            run_dropbox(FakeTransport(), {"query": ""})


class RegistrationTests(unittest.TestCase):
    def test_find_actions_are_registered(self):
        self.assertIn("slack_find", registry.ACTIONS)
        self.assertIn("slack_find_user", registry.ACTIONS)
        self.assertIn("dropbox_find", registry.ACTIONS)


class SlackFindOrCreateTests(unittest.TestCase):
    """create_if_missing on the channel branch — Zapier's Find or Create
    Channel: a missed name is reserved via conversations.create."""

    def test_channel_miss_creates_the_channel(self):
        transport = FakeTransport(
            {"ok": True, "channels": [{"id": "C1", "name": "general"}]},
            {"ok": True, "channel": {"id": "C9", "name": "incident-room",
                                     "is_private": True}},
        )
        output = run_slack(transport, {"find": "channel", "query": "#Incident Room",
                                       "create_if_missing": True, "is_private": True})

        self.assertEqual(output, {"found": True, "created": True, "channel": {
            "id": "C9", "name": "incident-room", "is_private": True}})
        create = transport.calls[1]
        self.assertEqual(create["url"], "https://slack.com/api/conversations.create")
        self.assertEqual(json.loads(create["body"]),
                         {"name": "incident-room", "is_private": True})

    def test_channel_hit_never_creates(self):
        transport = FakeTransport(
            {"ok": True, "channels": [{"id": "C2", "name": "docks"}]})
        output = run_slack(transport, {"find": "channel", "query": "docks",
                                       "create_if_missing": True})

        self.assertEqual(output["found"], True)
        self.assertNotIn("created", output)
        self.assertEqual(len(transport.calls), 1)

    def test_channel_miss_without_the_flag_stays_a_miss(self):
        transport = FakeTransport({"ok": True, "channels": []})
        output = run_slack(transport, {"find": "channel", "query": "missing"})

        self.assertEqual(output, {"found": False, "channel": None})
        self.assertEqual(len(transport.calls), 1)

    def test_user_miss_stays_a_miss_even_with_the_flag(self):
        transport = FakeTransport({"ok": False, "error": "users_not_found"})
        output = run_slack(transport, {"find": "user",
                                       "create_if_missing": True})

        self.assertEqual(output, {"found": False, "user": None})
        self.assertEqual(len(transport.calls), 1)

    def test_a_failed_create_raises(self):
        transport = FakeTransport(
            {"ok": True, "channels": []},
            {"ok": False, "error": "invalid_auth"},
        )
        with self.assertRaises(RuntimeError):
            run_slack(transport, {"find": "channel", "query": "new-channel",
                                  "create_if_missing": True})
