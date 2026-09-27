"""sheets_find_row: matching, find-or-create, and error paths."""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors.registry import run_action
from src.dapier.engine.actions.sheets import run_sheets_find_row


class FakeSheetsTransport:
    """GET serves a fixed worksheet dump; POST records the append call."""

    def __init__(self, rows=None, append_body=None):
        self.rows = rows if rows is not None else [["Task", "Status"]]
        self.append_body = append_body if append_body is not None else json.dumps({
            "spreadsheetId": "ss-1",
            "updates": {"spreadsheetId": "ss-1", "updatedRange": "todo!A97:B97",
                        "updatedRows": 1, "updatedColumns": 2, "updatedCells": 2},
        }).encode()
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if method == "GET":
            return 200, json.dumps({"values": self.rows}).encode()
        return 200, self.append_body


def run(transport, action, event=None, steps=None):
    action = {"type": "sheets_find_row", "connection_id": "google",
              "spreadsheet_id": "ss-1", "sheet_name": "todo",
              "match_field": "Task", "match_value": "call the dentist", **action}
    event = event or {
        "id": "e1", "connector": "telegram", "event": "message.received",
        "occurred_at": "2026-09-26T21:50:31+00:00",
        "data": {"text": "/todo call the dentist", "chat_id": 123},
    }
    with patch("src.dapier.engine.actions.sheets._sheets_connection",
               return_value={"connection_id": "google", "provider": "google",
                             "status": "connected"}), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("token-123", {})):
        return run_sheets_find_row(action, event, transport=transport, steps=steps)


class FindRowTests(unittest.TestCase):
    def test_finds_the_first_matching_row(self):
        transport = FakeSheetsTransport(rows=[
            ["Task", "Status"], ["buy milk", "NEW"], ["call the dentist", "DONE"]])
        output = run(transport, {"match_field": "status", "match_value": "DONE"})

        get_call = transport.calls[0]
        self.assertEqual(get_call["method"], "GET")
        self.assertIn("/spreadsheets/ss-1/values/todo%21A1%3AZZ10000", get_call["url"])
        self.assertEqual(get_call["headers"]["authorization"], "Bearer token-123")
        self.assertEqual(output, {
            "found": True, "row_number": 2,
            "row": {"Task": "call the dentist", "Status": "DONE"},
            "values": ["call the dentist", "DONE"],
        })
        self.assertEqual(len(transport.calls), 1)

    def test_match_value_renders_templates(self):
        transport = FakeSheetsTransport(
            rows=[["Task", "Status"], ["call the dentist", "NEW"]])
        output = run(transport, {"match_value": "{text|replace:/todo :|trim}"})
        self.assertTrue(output["found"])
        self.assertEqual(output["row"]["Task"], "call the dentist")

    def test_not_found_returns_found_false_without_creating(self):
        transport = FakeSheetsTransport(rows=[["Task", "Status"], ["buy milk", "NEW"]])
        output = run(transport, {})
        self.assertEqual(output, {"found": False, "row_number": None,
                                  "row": None, "values": None})
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_missing_match_column_fails(self):
        with self.assertRaises(ValueError):
            run(FakeSheetsTransport(), {"match_field": "  "})


class FindOrCreateTests(unittest.TestCase):
    def test_missing_row_is_appended_when_create_if_missing(self):
        transport = FakeSheetsTransport(rows=[["Task", "Status"]])
        output = run(transport, {
            "create_if_missing": True,
            "values": [["{text|replace:/todo :|trim}", "NEW"]],
        })

        self.assertEqual([call["method"] for call in transport.calls], ["GET", "POST"])
        post = transport.calls[1]
        self.assertIn("/values/todo:append", post["url"])
        self.assertIn("valueInputOption=USER_ENTERED", post["url"])
        self.assertEqual(json.loads(post["body"]),
                         {"values": [["call the dentist", "NEW"]]})
        self.assertEqual(output, {
            "found": False, "created": True, "row_number": None, "row": None,
            "values": ["call the dentist", "NEW"],
            "updated_range": "todo!A97:B97", "updated_cells": 2,
        })

    def test_designer_boolean_string_turns_creating_on(self):
        transport = FakeSheetsTransport(rows=[["Task"], ["buy milk"]])
        output = run(transport, {"create_if_missing": "true", "values": [["x"]]})
        self.assertEqual([call["method"] for call in transport.calls], ["GET", "POST"])
        self.assertTrue(output["created"])

    def test_creating_without_values_fails(self):
        transport = FakeSheetsTransport(rows=[["Task"], ["buy milk"]])
        with self.assertRaises(ValueError):
            run(transport, {"create_if_missing": True})


class RegistryDispatchTests(unittest.TestCase):
    """The registry entry is what the engine and trigger validation use."""

    def test_run_action_dispatches_sheets_find_row(self):
        transport = FakeSheetsTransport(rows=[["Task"], ["call the dentist"]])
        with patch("src.dapier.engine.actions.sheets._sheets_connection",
                   return_value={"connection_id": "google", "provider": "google",
                                 "status": "connected"}), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("token-123", {})), \
             patch("src.dapier.engine.actions.base._default_transport", transport):
            output = run_action(
                {"type": "sheets_find_row", "connection_id": "google",
                 "spreadsheet_id": "ss-1", "sheet_name": "todo",
                 "match_field": "Task", "match_value": "call the dentist"},
                {"data": {}}, "wf-1", steps={}, )
        self.assertTrue(output["found"])
        self.assertEqual(transport.calls[0]["method"], "GET")

    def test_registry_reports_the_runnable_signature(self):
        from src.dapier.connectors import registry

        required, optional = registry.action_specs()["sheets_find_row"]
        self.assertEqual(required, frozenset(
            {"connection_id", "spreadsheet_id", "match_field", "match_value"}))
        self.assertIn("create_if_missing", optional)
        self.assertIn("values", optional)


if __name__ == "__main__":
    unittest.main()
