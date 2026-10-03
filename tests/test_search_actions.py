"""Search-enabled actions: sheets_lookup_row, sheets_update_row and
slack_find_user — the find-then-act recipe (lookup a record, update it)."""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors.registry import (
    ActionError,
    catalog,
    run_action,
    validate_action_chain,
)
from src.dapier.engine.actions.sheets import (
    run_sheets_lookup_row,
    run_sheets_update_row,
)
from plugins.slack.runners.slack import run_slack_find_user


class FakeTransport:
    """One programmable response per call, recording everything."""

    def __init__(self, *responses):
        # A bare-bytes response counts as HTTP 200; (status, payload) tuples
        # set the status explicitly.
        self.responses = [
            response if isinstance(response, tuple) else (200, response)
            for response in responses
        ]
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body})
        status, payload = self.responses.pop(0) if self.responses else (200, b"{}")
        return status, payload if isinstance(payload, bytes) else payload.encode()


def _sheet_body(*rows):
    return json.dumps({"values": [list(row) for row in rows]}).encode()


def _connected():
    return (patch("src.dapier.engine.actions.sheets._sheets_connection",
                  return_value={"connection_id": "google", "provider": "google",
                                "status": "connected"}),
            patch("src.dapier.connections.tokens.get_access_token",
                  return_value=("token-123", {})))


EVENT = {"id": "e1", "connector": "email", "event": "message.received",
         "data": {"email": "a@x.y", "text": "b@x.y"}}


class SheetsLookupRowTests(unittest.TestCase):
    def lookup(self, transport, action):
        action = {"type": "sheets_lookup_row", "connection_id": "google",
                  "spreadsheet_id": "ss-1", "worksheet": "todo", **action}
        p1, p2 = _connected()
        with p1, p2:
            return run_sheets_lookup_row(action, EVENT, transport=transport)

    def test_finds_row_by_header_name(self):
        transport = FakeTransport(_sheet_body(
            ("Email", "Status"), ("a@x.y", "new"), ("b@x.y", "done")))
        output = self.lookup(transport, {"column": "email", "value": "a@x.y"})

        self.assertIn("/values/todo%21A1%3AZZ10000", transport.calls[0]["url"])
        self.assertEqual(output, {"found": True, "row": 2,
                                  "values": ["a@x.y", "new"],
                                  "matches": [{"row": 2, "values": ["a@x.y", "new"]}]})

    def test_finds_row_by_column_letter(self):
        transport = FakeTransport(_sheet_body(
            ("Email", "Status"), ("a@x.y", "new"), ("b@x.y", "done")))
        output = self.lookup(transport, {"column": "B", "value": "done"})
        self.assertEqual(output["found"], True)
        self.assertEqual(output["row"], 3)

    def test_value_is_template_rendered(self):
        transport = FakeTransport(_sheet_body(
            ("Email", "Status"), ("a@x.y", "new")))
        output = self.lookup(transport, {"column": "Email", "value": "{email}"})
        self.assertEqual(output["row"], 2)

    def test_match_is_trimmed_and_case_sensitive(self):
        rows = ("Email",), ("a@x.y",), ("A@X.Y",)
        self.assertEqual(self.lookup(FakeTransport(_sheet_body(*rows)),
                                     {"column": "A", "value": "a@x.y"})["row"], 2)
        self.assertEqual(self.lookup(FakeTransport(_sheet_body(*rows)),
                                     {"column": "A", "value": " A@X.Y "})["row"], 3)

    def test_miss_is_a_result_not_an_error(self):
        transport = FakeTransport(_sheet_body(("Email",), ("a@x.y",)))
        output = self.lookup(transport, {"column": "Email", "value": "nope"})
        self.assertEqual(output, {"found": False, "row": None, "values": None,
                                  "matches": []})

    def test_limit_caps_matches(self):
        rows = ("Email", "Status"), ("b@x.y", "one"), ("b@x.y", "two"), ("b@x.y", "three")
        limited = self.lookup(FakeTransport(_sheet_body(*rows)),
                              {"column": "Email", "value": "b@x.y", "limit": 2})
        self.assertEqual([match["row"] for match in limited["matches"]], [2, 3])
        self.assertEqual(limited["values"], ["b@x.y", "one"])
        default = self.lookup(FakeTransport(_sheet_body(*rows)),
                              {"column": "Email", "value": "b@x.y"})
        self.assertEqual(len(default["matches"]), 1)

    def test_unknown_header_name_fails(self):
        transport = FakeTransport(_sheet_body(("Email",), ("a@x.y",)))
        with self.assertRaises(ValueError) as caught:
            self.lookup(transport, {"column": "Task", "value": "a@x.y"})
        self.assertIn("Task", str(caught.exception))

    def test_missing_column_or_value_fails(self):
        with self.assertRaises(ValueError):
            self.lookup(FakeTransport(), {"column": " ", "value": "x"})
        with self.assertRaises(ValueError):
            self.lookup(FakeTransport(), {"column": "A", "value": ""})

    def test_bad_limit_fails(self):
        with self.assertRaises(ValueError):
            self.lookup(FakeTransport(), {"column": "A", "value": "x", "limit": "many"})


class SheetsUpdateRowTests(unittest.TestCase):
    def update(self, transport, action):
        action = {"type": "sheets_update_row", "connection_id": "google",
                  "spreadsheet_id": "ss-1", "worksheet": "todo", **action}
        p1, p2 = _connected()
        with p1, p2:
            return run_sheets_update_row(action, EVENT, transport=transport)

    def test_updates_the_row_with_rendered_cells(self):
        transport = FakeTransport(_sheet_body(("updates",)))
        output = self.update(transport, {
            "row": 7,
            "values": [["{email}", "{text}", "", "DONE"]],
        })

        call = transport.calls[0]
        self.assertEqual(call["method"], "PUT")
        self.assertIn("/spreadsheets/ss-1/values/todo%21A7", call["url"])
        self.assertIn("valueInputOption=USER_ENTERED", call["url"])
        self.assertEqual(call["headers"]["authorization"], "Bearer token-123")
        self.assertEqual(json.loads(call["body"]), {"values": [["a@x.y", "b@x.y", "", "DONE"]]})
        self.assertEqual(output, {"row": 7, "updated": True})

    def test_values_json_string_and_flat_row_are_accepted(self):
        transport = FakeTransport(b"{}")
        output = self.update(transport, {"row": 2, "values": '["{email}", "DONE"]'})
        self.assertEqual(json.loads(transport.calls[0]["body"]),
                         {"values": [["a@x.y", "DONE"]]})
        self.assertEqual(output, {"row": 2, "updated": True})

    def test_raw_input_option_is_passed_through(self):
        transport = FakeTransport(b"{}")
        self.update(transport, {"row": 2, "values": [["x"]], "value_input_option": "raw"})
        self.assertIn("valueInputOption=RAW", transport.calls[0]["url"])

    def test_multi_row_values_fail(self):
        with self.assertRaises(ValueError):
            self.update(FakeTransport(), {"row": 2, "values": [["a"], ["b"]]})

    def test_bad_row_numbers_fail(self):
        with self.assertRaises(ValueError):
            self.update(FakeTransport(), {"row": "second", "values": [["x"]]})
        with self.assertRaises(ValueError):
            self.update(FakeTransport(), {"row": 0, "values": [["x"]]})

    def test_http_error_surfaces_status_and_message(self):
        transport = FakeTransport((400, json.dumps(
            {"error": {"code": 400, "message": "Unable to parse range: todo!A0"}}).encode()))
        with self.assertRaises(RuntimeError) as caught:
            self.update(transport, {"row": 2, "values": [["x"]]})
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("Unable to parse range", str(caught.exception))


class SlackFindUserTests(unittest.TestCase):
    def find(self, transport, action, event=None):
        action = {"type": "slack_find_user", "connection_id": "slack", **action}
        with patch("plugins.slack.runners.slack._token_for",
                   return_value="xoxb-token"):
            return run_slack_find_user(action, event or EVENT, transport=transport)

    def test_finds_the_user_by_rendered_email(self):
        transport = FakeTransport(json.dumps({"ok": True, "user": {
            "id": "U1", "name": "jo", "real_name": "Jo Example",
            "tz": "Europe/Berlin", "profile": {"email": "a@x.y"},
        }}).encode())
        output = self.find(transport, {"email": "{email}"})

        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://slack.com/api/users.lookupByEmail")
        self.assertEqual(call["headers"]["authorization"], "Bearer xoxb-token")
        self.assertEqual(json.loads(call["body"]), {"email": "a@x.y"})
        self.assertEqual(output, {"found": True, "user": {
            "id": "U1", "name": "jo", "real_name": "Jo Example",
            "email": "a@x.y", "tz": "Europe/Berlin"}})

    def test_miss_is_a_result_not_an_error(self):
        transport = FakeTransport(b'{"ok": false, "error": "users_not_found"}')
        output = self.find(transport, {"email": "nobody@x.y"})
        self.assertEqual(output, {"found": False, "user": None})

    def test_other_slack_errors_raise(self):
        transport = FakeTransport(b'{"ok": false, "error": "invalid_auth"}')
        with self.assertRaises(RuntimeError) as caught:
            self.find(transport, {"email": "a@x.y"})
        self.assertIn("invalid_auth", str(caught.exception))

    def test_empty_rendered_email_fails(self):
        with self.assertRaises(ValueError):
            self.find(FakeTransport(), {"email": " {missing} "})


class RegistryTests(unittest.TestCase):
    """The registry entries are what the engine, trigger validation and
    GET /api/catalog consume."""

    def test_run_action_dispatches_sheets_update_row(self):
        transport = FakeTransport(b"{}")
        p1, p2 = _connected()
        with p1, p2, \
             patch("src.dapier.engine.actions.base._default_transport", transport):
            output = run_action(
                {"type": "sheets_update_row", "connection_id": "google",
                 "spreadsheet_id": "ss-1", "worksheet": "todo", "row": 3,
                 "values": [["a", "b"]]},
                {"data": {}}, "wf-1", steps={})
        self.assertEqual(output, {"row": 3, "updated": True})
        self.assertEqual(transport.calls[0]["method"], "PUT")

    def test_run_action_dispatches_slack_find_user(self):
        transport = FakeTransport(b'{"ok": false, "error": "users_not_found"}')
        with patch("plugins.slack.runners.slack._token_for",
                   return_value="xoxb-token"), \
             patch("src.dapier.engine.actions.base._default_transport", transport):
            output = run_action(
                {"type": "slack_find_user", "connection_id": "slack",
                 "email": "nobody@x.y"},
                {"data": {}}, "wf-1", steps={})
        self.assertEqual(output, {"found": False, "user": None})

    def test_specs_report_required_and_optional_keys(self):
        from src.dapier.connectors import registry

        specs = registry.action_specs()
        self.assertEqual(specs["sheets_lookup_row"],
                         (frozenset({"connection_id", "spreadsheet_id", "worksheet",
                                     "column", "value"}),
                          frozenset({"limit"})))
        self.assertEqual(specs["sheets_update_row"],
                         (frozenset({"connection_id", "spreadsheet_id", "worksheet",
                                     "row", "values"}),
                          frozenset({"value_input_option"})))
        self.assertEqual(specs["slack_find_user"],
                         (frozenset({"connection_id", "email"}),
                          frozenset({"credential_id"})))

    def test_chain_validation_accepts_find_then_update(self):
        validate_action_chain([
            {"type": "sheets_lookup_row", "connection_id": "google",
             "spreadsheet_id": "ss-1", "worksheet": "todo", "column": "Email",
             "value": "{email}"},
            {"type": "sheets_update_row", "connection_id": "google",
             "spreadsheet_id": "ss-1", "worksheet": "todo",
             "row": "{steps.lookup.output.row}",
             "values": ["{email}", "DONE"]},
            {"type": "slack_find_user", "connection_id": "slack",
             "email": "{steps.lookup.output.values.0}"},
        ])

    def test_chain_validation_still_accepts_existing_chains(self):
        validate_action_chain([
            {"type": "sheets_append_row", "connection_id": "google",
             "spreadsheet_id": "ss-1", "sheet_name": "todo",
             "values": [["{text}", "NEW"]]},
            {"type": "slack", "channel": "#alerts", "text": "{title}"},
        ])

    def test_chain_validation_rejects_unknown_and_missing_keys(self):
        with self.assertRaises(ActionError) as caught:
            validate_action_chain([
                {"type": "sheets_lookup_row", "connection_id": "google",
                 "spreadsheet_id": "ss-1", "worksheet": "todo", "column": "Email",
                 "value": "x", "bogus": "y"}])
        self.assertIn("bogus", str(caught.exception))
        with self.assertRaises(ActionError) as caught:
            validate_action_chain([
                {"type": "slack_find_user", "connection_id": "slack"}])
        self.assertIn("email", str(caught.exception))

    def test_catalog_exposes_the_new_actions(self):
        entries = {entry["type"]: entry for entry in catalog()["actions"]}
        for action_type, field_keys in (
            ("sheets_lookup_row", {"connection_id", "spreadsheet_id", "worksheet",
                                   "column", "value", "limit"}),
            ("sheets_update_row", {"connection_id", "spreadsheet_id", "worksheet",
                                   "row", "values", "value_input_option"}),
            ("slack_find_user", {"connection_id", "email", "credential_id"}),
        ):
            self.assertIn(action_type, entries)
            self.assertEqual(
                {field["key"] for field in entries[action_type]["fields"]},
                field_keys)


if __name__ == "__main__":
    unittest.main()
