"""sheets_find_row: found/not-found output, create-if-missing appends, and
error paths — the Zapier Find-or-create Spreadsheet Row semantics."""
import json
import unittest
from unittest.mock import patch

from src.dapier.engine.actions.sheets import run_sheets_find_row


class FakeTransport:
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
            "updates": {"spreadsheetId": "ss-1", "updatedRange": "todo!A6:B6",
                        "updatedRows": 1, "updatedColumns": 2, "updatedCells": 2},
        }).encode()


SHEET = [["Task", "Status"], ["write blog post", "NEW"],
         ["call the dentist", "OPEN"], ["ship release", "NEW"]]


def run_find(transport, action, event=None):
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
        return run_sheets_find_row(action, event, transport=transport)


class FindRowTests(unittest.TestCase):
    def test_finds_the_first_row_matching_the_column(self):
        transport = FakeTransport(rows=SHEET)
        output = run_find(transport, {})

        self.assertEqual(transport.calls[0]["method"], "GET")
        self.assertIn("/spreadsheets/ss-1/values/todo%21A1%3AZZ10000",
                      transport.calls[0]["url"])
        self.assertEqual(transport.calls[0]["headers"]["authorization"],
                         "Bearer token-123")
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])
        self.assertEqual(output, {"found": True, "row_number": 2,
                                  "row": {"Task": "call the dentist", "Status": "OPEN"},
                                  "values": ["call the dentist", "OPEN"]})

    def test_header_match_is_trimmed_and_case_insensitive(self):
        transport = FakeTransport(rows=SHEET)
        output = run_find(transport, {"match_field": "  task "})
        self.assertTrue(output["found"])
        self.assertEqual(output["row_number"], 2)

    def test_unknown_match_field_reports_not_found(self):
        transport = FakeTransport(rows=SHEET)
        output = run_find(transport, {"match_field": "Priority"})

        self.assertEqual(output, {"found": False, "row_number": None,
                                  "row": None, "values": None})
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_no_match_reports_found_false_without_appending(self):
        transport = FakeTransport(rows=SHEET)
        output = run_find(transport, {"match_value": "never written"})

        self.assertEqual(output, {"found": False, "row_number": None,
                                  "row": None, "values": None})
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_empty_spreadsheet_is_not_found_not_an_error(self):
        transport = FakeTransport(rows=[])
        output = run_find(transport, {})
        self.assertEqual(output["found"], False)


class CreateIfMissingTests(unittest.TestCase):
    def test_create_if_missing_appends_the_rendered_row(self):
        transport = FakeTransport(rows=SHEET)
        output = run_find(transport, {"match_value": "never written",
                                      "create_if_missing": True,
                                      "values": [["{text|replace:/todo :|trim}",
                                                  "NEW"]]})

        self.assertEqual([call["method"] for call in transport.calls], ["GET", "POST"])
        append = transport.calls[1]
        self.assertIn(":append", append["url"])
        self.assertIn("valueInputOption=USER_ENTERED", append["url"])
        self.assertEqual(json.loads(append["body"]),
                         {"values": [["call the dentist", "NEW"]]})
        self.assertEqual(output, {"found": False, "created": True, "row_number": None,
                                  "row": None, "values": ["call the dentist", "NEW"],
                                  "updated_range": "todo!A6:B6", "updated_cells": 2})

    def test_create_if_missing_without_values_fails(self):
        with self.assertRaises(ValueError) as caught:
            run_find(FakeTransport(rows=SHEET),
                     {"match_value": "never written", "create_if_missing": True})
        self.assertIn("sheets_find_row needs values", str(caught.exception))

    def test_create_if_missing_false_never_appends(self):
        transport = FakeTransport(rows=SHEET)
        output = run_find(transport, {"match_value": "never written",
                                      "create_if_missing": "false",
                                      "values": [["never written", "NEW"]]})

        self.assertEqual([call["method"] for call in transport.calls], ["GET"])
        self.assertNotIn("created", output)


class FindRowErrorTests(unittest.TestCase):
    def test_read_http_error_surfaces_status_and_message(self):
        transport = FakeTransport(rows=SHEET, status=403)
        with self.assertRaises(RuntimeError) as caught:
            run_find(transport, {})
        self.assertIn("HTTP 403", str(caught.exception))

    def test_unreachable_api_fails(self):
        def transport(method, url, *, headers, body, timeout=15):
            raise OSError("no network")

        with self.assertRaises(RuntimeError) as caught:
            run_find(transport, {})
        self.assertIn("unreachable", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
