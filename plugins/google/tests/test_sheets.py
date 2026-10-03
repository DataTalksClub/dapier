"""sheets_append_row / sheets_find_row: rendering, Sheets API call shape,
and error paths."""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors.registry import run_action
from plugins.google.runners.sheets import run_sheets_append_row, run_sheets_find_row


class FakeTransport:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else json.dumps({
            "spreadsheetId": "ss-1",
            "updates": {"spreadsheetId": "ss-1", "updatedRange": "todo!A96:D96",
                        "updatedRows": 1, "updatedColumns": 4, "updatedCells": 4},
        }).encode()
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return self.status, self.body


def run(transport, action, event=None):
    action = {"type": "sheets_append_row", "connection_id": "google",
              "spreadsheet_id": "ss-1", "sheet_name": "todo", **action}
    event = event or {
        "id": "e1", "connector": "telegram", "event": "message.received",
        "occurred_at": "2026-09-26T21:50:31+00:00",
        "data": {"text": "/todo call the dentist", "chat_id": 123},
    }
    with patch("plugins.google.runners.sheets._sheets_connection",
               return_value={"connection_id": "google", "provider": "google",
                             "status": "connected"}), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("token-123", {})):
        return run_sheets_append_row(action, event, transport=transport)


class AppendRowTests(unittest.TestCase):
    def test_appends_rendered_row_to_the_worksheet(self):
        transport = FakeTransport()
        output = run(transport, {
            "values": [["{trigger.occurred_at|date_format:%Y-%m-%d}",
                        "{text|replace:/todo :|trim}", "", "NEW"]],
        })

        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/spreadsheets/ss-1/values/todo:append", call["url"])
        self.assertIn("valueInputOption=USER_ENTERED", call["url"])
        self.assertIn("insertDataOption=INSERT_ROWS", call["url"])
        self.assertEqual(call["headers"]["authorization"], "Bearer token-123")
        body = json.loads(call["body"])
        self.assertEqual(body, {"values": [["2026-09-26", "call the dentist", "", "NEW"]]})
        self.assertEqual(output, {"spreadsheet_id": "ss-1", "updated_range": "todo!A96:D96",
                                  "updated_rows": 1, "updated_cells": 4})

    def test_cell_text_containing_quotes_stays_intact(self):
        transport = FakeTransport()
        run(transport, {"values": [["task with \"quotes\" and {braces", "NEW"]]})
        body = json.loads(transport.calls[0]["body"])
        self.assertEqual(body["values"][0][0], 'task with "quotes" and {braces')

    def test_values_json_string_and_flat_single_row_are_accepted(self):
        transport = FakeTransport()
        run(transport, {"values": '["{text|replace:/todo :|trim}", "NEW"]'})
        body = json.loads(transport.calls[0]["body"])
        self.assertEqual(body["values"], [["call the dentist", "NEW"]])

    def test_empty_worksheet_falls_back_to_sheet1(self):
        transport = FakeTransport()
        run(transport, {"values": [["x"]], "sheet_name": ""})
        self.assertIn("/values/Sheet1:append", transport.calls[0]["url"])

    def test_raw_input_option_is_passed_through(self):
        transport = FakeTransport()
        run(transport, {"values": [["x"]], "value_input_option": "raw"})
        self.assertIn("valueInputOption=RAW", transport.calls[0]["url"])

    def test_unknown_input_option_fails(self):
        with self.assertRaises(ValueError):
            run(FakeTransport(), {"values": [["x"]], "value_input_option": "LABEL"})

    def test_invalid_values_json_fails(self):
        with self.assertRaises(ValueError):
            run(FakeTransport(), {"values": "{text"})

    def test_non_array_values_fail(self):
        with self.assertRaises(ValueError):
            run(FakeTransport(), {"values": {"Task": "{text}"}})

    def test_mixed_row_shapes_fail(self):
        with self.assertRaises(ValueError):
            run(FakeTransport(), {"values": [["a"], "b"]})


class AppendRowErrorTests(unittest.TestCase):
    def test_http_error_surfaces_status_and_message(self):
        transport = FakeTransport(status=400, body=json.dumps(
            {"error": {"code": 400, "message": "Unable to parse range: todo",
                       "status": "INVALID_ARGUMENT"}}).encode())
        with self.assertRaises(RuntimeError) as caught:
            run(transport, {"values": [["x"]]})
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("Unable to parse range: todo", str(caught.exception))

    def test_unreachable_api_fails(self):
        def transport(method, url, *, headers, body, timeout=15):
            raise OSError("no network")

        with self.assertRaises(RuntimeError) as caught:
            run(transport, {"values": [["x"]]})
        self.assertIn("unreachable", str(caught.exception))


class RegistryDispatchTests(unittest.TestCase):
    """The registry entry is what the engine and trigger validation use."""

    def test_run_action_dispatches_sheets_append_row(self):
        transport = FakeTransport()
        with patch("plugins.google.runners.sheets._sheets_connection",
                   return_value={"connection_id": "google", "provider": "google",
                                 "status": "connected"}), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("token-123", {})), \
             patch("src.dapier.engine.actions.base._default_transport", transport):
            output = run_action(
                {"type": "sheets_append_row", "connection_id": "google",
                 "spreadsheet_id": "ss-1", "sheet_name": "todo",
                 "values": [["hello", "NEW"]]},
                {"data": {}}, "wf-1", steps={}, )
        self.assertEqual(output["spreadsheet_id"], "ss-1")
        self.assertEqual(transport.calls[0]["method"], "POST")

    def test_registry_reports_the_runnable_signature(self):
        from src.dapier.connectors import registry

        specs = registry.action_specs()
        required, optional = specs["sheets_append_row"]
        self.assertEqual(required, frozenset({"connection_id", "spreadsheet_id", "values"}))
        self.assertIn("sheet_name", optional)


class FindTransport:
    """Serves one values.get payload and records every call, including appends."""

    def __init__(self, rows=None, status=200):
        self.rows = rows if rows is not None else []
        self.status = status
        self.calls = []

    def __call__(self, method, url, *, headers, body, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
        if method == "GET":
            return self.status, json.dumps({"values": self.rows}).encode()
        return self.status, json.dumps({
            "spreadsheetId": "ss-1",
            "updates": {"spreadsheetId": "ss-1", "updatedRange": "todo!A5:B5",
                        "updatedRows": 1, "updatedColumns": 2, "updatedCells": 2},
        }).encode()


SHEET = [["Task", "Status"], ["write blog post", "NEW"], ["call the dentist", "OPEN"]]


def run_find(transport, action, event=None):
    action = {"type": "sheets_find_row", "connection_id": "google",
              "spreadsheet_id": "ss-1", "sheet_name": "todo",
              "match_field": "Task", "match_value": "call the dentist", **action}
    event = event or {
        "id": "e1", "connector": "telegram", "event": "message.received",
        "occurred_at": "2026-09-26T21:50:31+00:00",
        "data": {"text": "/todo call the dentist", "chat_id": 123},
    }
    with patch("plugins.google.runners.sheets._sheets_connection",
               return_value={"connection_id": "google", "provider": "google",
                             "status": "connected"}), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("token-123", {})):
        return run_sheets_find_row(action, event, transport=transport)


class FindRowTests(unittest.TestCase):
    def test_finds_the_first_row_matching_the_column(self):
        transport = FindTransport(rows=SHEET)
        output = run_find(transport, {})

        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertIn("/spreadsheets/ss-1/values/todo%21A1%3AZZ10000",
                      transport.calls[0]["url"])
        self.assertEqual(transport.calls[0]["headers"]["authorization"],
                         "Bearer token-123")
        self.assertTrue(output["found"])
        self.assertEqual(output["row_number"], 2)
        self.assertEqual(output["row"], {"Task": "call the dentist", "Status": "OPEN"})
        self.assertEqual(output["values"], ["call the dentist", "OPEN"])

    def test_header_match_is_trimmed_and_case_insensitive(self):
        transport = FindTransport(rows=SHEET)
        output = run_find(transport, {"match_field": "  task "})
        self.assertTrue(output["found"])

    def test_match_value_is_template_rendered(self):
        transport = FindTransport(rows=SHEET)
        output = run_find(transport, {"match_value": "{text|replace:/todo :|trim}"})
        self.assertTrue(output["found"])
        self.assertEqual(output["row"]["Task"], "call the dentist")

    def test_no_match_reports_found_false_without_appending(self):
        transport = FindTransport(rows=SHEET)
        output = run_find(transport, {"match_value": "never written"})

        self.assertEqual(output, {"found": False, "row_number": None,
                                  "row": None, "values": None})
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_create_if_missing_appends_the_missing_row(self):
        transport = FindTransport(rows=SHEET)
        output = run_find(transport, {"match_value": "never written",
                                      "create_if_missing": True,
                                      "values": [["never written", "NEW"]]})

        self.assertEqual([call["method"] for call in transport.calls], ["GET", "POST"])
        self.assertIn(":append", transport.calls[1]["url"])
        self.assertEqual(json.loads(transport.calls[1]["body"]),
                         {"values": [["never written", "NEW"]]})
        self.assertEqual(output, {"found": False, "created": True, "row_number": None,
                                  "row": None, "values": ["never written", "NEW"],
                                  "updated_range": "todo!A5:B5", "updated_cells": 2})

    def test_create_if_missing_string_false_does_not_append(self):
        transport = FindTransport(rows=SHEET)
        output = run_find(transport, {"match_value": "never written",
                                      "create_if_missing": "false",
                                      "values": [["never written", "NEW"]]})

        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("created", output)

    def test_create_if_missing_without_values_fails(self):
        with self.assertRaises(ValueError):
            run_find(FindTransport(rows=SHEET),
                     {"match_value": "never written", "create_if_missing": True})

    def test_empty_rendered_match_value_fails(self):
        with self.assertRaises(ValueError):
            run_find(FindTransport(rows=SHEET), {"match_value": "{data.missing}"})

    def test_missing_match_field_fails(self):
        with self.assertRaises(ValueError):
            run_find(FindTransport(rows=SHEET), {"match_field": ""})

    def test_read_http_error_surfaces_status_and_message(self):
        transport = FindTransport(rows=SHEET, status=403)
        with self.assertRaises(RuntimeError) as caught:
            run_find(transport, {})
        self.assertIn("HTTP 403", str(caught.exception))


class FindRowRegistryTests(unittest.TestCase):
    def test_run_action_dispatches_sheets_find_row(self):
        transport = FindTransport(rows=SHEET)
        with patch("plugins.google.runners.sheets._sheets_connection",
                   return_value={"connection_id": "google", "provider": "google",
                                 "status": "connected"}), \
             patch("src.dapier.connections.tokens.get_access_token",
                   return_value=("token-123", {})), \
             patch("src.dapier.engine.actions.base._default_transport", transport):
            output = run_action(
                {"type": "sheets_find_row", "connection_id": "google",
                 "spreadsheet_id": "ss-1", "sheet_name": "todo",
                 "match_field": "Task", "match_value": "write blog post"},
                {"data": {}}, "wf-1", steps={})

        self.assertTrue(output["found"])
        self.assertEqual(output["row"]["Task"], "write blog post")
        self.assertEqual(transport.calls[0]["method"], "GET")

    def test_registry_reports_the_find_signature(self):
        from src.dapier.connectors import registry

        required, optional = registry.action_specs()["sheets_find_row"]
        self.assertEqual(required, frozenset(
            {"connection_id", "spreadsheet_id", "match_field", "match_value"}))
        self.assertEqual(
            optional,
            frozenset({"sheet_name", "create_if_missing", "values", "value_input_option"}))


if __name__ == "__main__":
    unittest.main()
