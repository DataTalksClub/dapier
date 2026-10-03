"""Action-staple and trigger-option tests for the Drive/Sheets round:
drive_find_file's match list (files + next_page_token), drive_delete_file,
drive_create_folder, sheets_clear_values, and the google-drive.folders /
google-sheets.worksheets trigger options.

Unit tests drive each registered runner with a fake provider transport and
the connection/token seams patched (the test_round_yts3zoom /
test_round_sheetstelegram pattern), asserting method, URL, request body and
the step-output shape. The options half follows test_trigger_samples: a
canned transport at the shared provider seam and a fake connections table,
with the two new resources pinned in trigger_discovery_catalog().
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from src.dapier.connectors import trigger_discovery
from plugins.google.runners.drive import (
    run_drive_create_folder,
    run_drive_delete_file,
    run_drive_find_file,
)
from plugins.google.runners.sheets import run_sheets_clear_values

import src.dapier.connectors  # noqa: F401  (import = registration)


GOOGLE_CONNECTION = {"connection_id": "google", "provider": "google",
                     "status": "connected", "credential_id": "oauth#google"}

EVENT = {"connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"subject": "Invoices 2026"}}

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"


class FakeTransport:
    """Records every call; routes by URL substring to (status, body bytes)."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, body) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body, "timeout": timeout})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, payload
        raise AssertionError(f"unexpected provider call: {method} {url}")


def json_body(payload):
    return json.dumps(payload).encode()


# --- drive_find_file: the match list ---------------------------------------------


def run_find(transport, action, event=None, steps=None):
    action = {"type": "drive_find_file", "connection_id": "google", **action}
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value=dict(GOOGLE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_drive_find_file(action, event or EVENT, steps=steps,
                                   transport=transport)


class DriveFindFileTests(unittest.TestCase):
    def routes_hits(self, hits, next_page_token=None):
        page = {"files": hits}
        if next_page_token:
            page["nextPageToken"] = next_page_token
        return FakeTransport(("drive/v3/files", 200, json_body(page)))

    def test_lists_every_match_and_keeps_the_old_keys(self):
        transport = self.routes_hits([
            {"id": "f-2", "name": "report-q3.pdf", "mimeType": "application/pdf",
             "modifiedTime": "2026-09-28T10:00:00.000Z"},
            {"id": "f-1", "name": "report-q1.pdf", "mimeType": "application/pdf",
             "modifiedTime": "2026-09-27T10:00:00.000Z"},
        ], next_page_token="tok-9")

        output = run_find(transport, {"name": "report"})

        self.assertEqual(output, {
            "found": True,
            "file": {"id": "f-2", "name": "report-q3.pdf",
                     "mimeType": "application/pdf",
                     "modified": "2026-09-28T10:00:00.000Z"},
            "count": 2,
            "files": [
                {"id": "f-2", "name": "report-q3.pdf",
                 "mimeType": "application/pdf",
                 "modified": "2026-09-28T10:00:00.000Z"},
                {"id": "f-1", "name": "report-q1.pdf",
                 "mimeType": "application/pdf",
                 "modified": "2026-09-27T10:00:00.000Z"},
            ],
            "next_page_token": "tok-9",
        })
        self.assertEqual(output["file"], output["files"][0])  # the same shape

    def test_a_page_without_a_token_has_none(self):
        transport = self.routes_hits([
            {"id": "f-1", "name": "report.pdf", "mimeType": "application/pdf",
             "modifiedTime": "2026-09-27T10:00:00.000Z"}])

        output = run_find(transport, {"name": "report"})

        self.assertIsNone(output["next_page_token"])
        self.assertEqual(len(output["files"]), 1)

    def test_a_miss_lists_nothing(self):
        transport = self.routes_hits([])

        output = run_find(transport, {"name": "no-such-file"})

        self.assertEqual(output, {"found": False, "file": None, "count": 0,
                                  "files": [], "next_page_token": None})

    def test_name_and_folder_take_templates(self):
        transport = self.routes_hits([
            {"id": "f-3", "name": "Standup notes", "mimeType": None,
             "modifiedTime": "2026-09-26T08:00:00.000Z"}])

        output = run_find(
            transport, {"name": "{subject} notes", "folder": "{folder_id}",
                        "match": "exact"},
            event={"data": {"subject": "Standup", "folder_id": "fold-7"}},
            steps={})

        self.assertEqual(output["count"], 1)
        call, = transport.calls
        self.assertIn("q=trashed%3Dfalse+and+name+%3D+%27Standup+notes%27",
                      call["url"])
        self.assertIn("%27fold-7%27+in+parents", call["url"])


# --- drive_delete_file -------------------------------------------------------------


def run_delete(transport, action, event=None, steps=None):
    action = {"type": "drive_delete_file", "connection_id": "google", **action}
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value=dict(GOOGLE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_drive_delete_file(action, event or EVENT, steps=steps,
                                     transport=transport)


class DriveDeleteFileTests(unittest.TestCase):
    def test_trashes_the_file_instead_of_destroying_it(self):
        transport = FakeTransport(
            ("drive/v3/files", 200,
             json_body({"id": "f-1", "name": "report.pdf", "trashed": True})))

        output = run_delete(transport, {"file_id": "f-1"})

        self.assertEqual(output, {"trashed": True, "permanent": False,
                                  "file_id": "f-1", "name": "report.pdf"})
        call, = transport.calls
        self.assertEqual(call["method"], "PATCH")
        self.assertTrue(call["url"].startswith(DRIVE_FILES_URL + "/f-1?"))
        self.assertIn("supportsAllDrives=true", call["url"])
        self.assertIn("fields=id%2Cname%2Ctrashed", call["url"])
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(call["body"]), {"trashed": True})

    def test_file_id_takes_a_find_step_template(self):
        transport = FakeTransport(
            ("drive/v3/files", 200, json_body({"id": "f-9"})))

        output = run_delete(transport, {"file_id": "{steps.find.output.file.id}"},
                            steps={"find": {"output": {"file": {"id": "f-9"}}}})

        self.assertEqual(output["file_id"], "f-9")
        self.assertTrue(transport.calls[0]["url"].startswith(
            DRIVE_FILES_URL + "/f-9?"))

    def test_requires_a_file_id(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError):
            run_delete(transport, {})
        self.assertEqual(transport.calls, [])

    def test_errors_surface_the_api_message(self):
        transport = FakeTransport(
            ("drive/v3/files", 404,
             json_body({"error": {"message": "File not found"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_delete(transport, {"file_id": "f-gone"})
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertIn("File not found", str(caught.exception))


# --- drive_create_folder -----------------------------------------------------------


def run_create_folder(transport, action, event=None, steps=None):
    action = {"type": "drive_create_folder", "connection_id": "google", **action}
    with patch("src.dapier.engine.actions.base._connected_connection",
               return_value=dict(GOOGLE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_drive_create_folder(action, event or EVENT, steps=steps,
                                       transport=transport)


class DriveCreateFolderTests(unittest.TestCase):
    def test_creates_a_folder_at_the_drive_root(self):
        transport = FakeTransport(
            ("drive/v3/files", 200, json_body({
                "id": "fold-new",
                "name": "Invoices 2026",
                "webViewLink": "https://drive.google.com/drive/folders/fold-new"})))

        output = run_create_folder(transport, {"name": "Invoices 2026"})

        self.assertEqual(output, {
            "folder_id": "fold-new", "name": "Invoices 2026",
            "url": "https://drive.google.com/drive/folders/fold-new"})
        call, = transport.calls
        self.assertEqual(call["method"], "POST")
        self.assertTrue(call["url"].startswith(DRIVE_FILES_URL + "?"))
        self.assertIn("fields=id%2Cname%2CwebViewLink", call["url"])
        self.assertEqual(json.loads(call["body"]), {
            "name": "Invoices 2026",
            "mimeType": "application/vnd.google-apps.folder",
        })

    def test_parent_folder_and_templated_name(self):
        transport = FakeTransport(
            ("drive/v3/files", 200, json_body({"id": "fold-2"})))

        output = run_create_folder(
            transport, {"name": "{subject}", "parent_folder_id": "fold-1"},
            event={"data": {"subject": "Standups"}})

        self.assertEqual(output["name"], "Standups")
        self.assertIsNone(output["url"])  # the response carried no webViewLink
        self.assertEqual(json.loads(transport.calls[0]["body"]), {
            "name": "Standups",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["fold-1"],
        })

    def test_requires_a_name(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError):
            run_create_folder(transport, {"parent_folder_id": "fold-1"})
        self.assertEqual(transport.calls, [])

    def test_errors_surface_the_api_message(self):
        transport = FakeTransport(
            ("drive/v3/files", 404,
             json_body({"error": {"message": "parent not found"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_create_folder(transport, {"name": "X",
                                          "parent_folder_id": "fold-gone"})
        self.assertIn("HTTP 404", str(caught.exception))
        self.assertIn("parent not found", str(caught.exception))


# --- sheets_clear_values ------------------------------------------------------------


def run_clear(transport, action, event=None, steps=None):
    action = {"type": "sheets_clear_values", "connection_id": "google", **action}
    with patch("plugins.google.runners.sheets._sheets_connection",
               return_value=dict(GOOGLE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_sheets_clear_values(action, event or EVENT, steps=steps,
                                       transport=transport)


class SheetsClearValuesTests(unittest.TestCase):
    def test_defaults_to_the_whole_worksheet(self):
        transport = FakeTransport(
            ("values", 200, json_body({"spreadsheetId": "ss-1",
                                       "clearedRange": "todo!A1:ZZ10000"})))

        output = run_clear(transport, {"spreadsheet_id": "ss-1",
                                       "worksheet": "todo"})

        self.assertEqual(output, {"cleared_range": "todo!A1:ZZ10000",
                                  "spreadsheet_id": "ss-1"})
        call, = transport.calls
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"],
            SHEETS_URL + "/ss-1/values/todo%21A1%3AZZ10000:clear")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertIsNone(call["body"])

    def test_a_bare_range_is_qualified_with_the_worksheet(self):
        transport = FakeTransport(
            ("values", 200, json_body({"spreadsheetId": "ss-1",
                                       "clearedRange": "Invoices!A2:Z100"})))

        output = run_clear(transport, {"spreadsheet_id": "ss-1",
                                       "worksheet": "Invoices",
                                       "range": "A2:Z100"})

        self.assertEqual(output["cleared_range"], "Invoices!A2:Z100")
        self.assertEqual(
            transport.calls[0]["url"],
            SHEETS_URL + "/ss-1/values/Invoices%21A2%3AZ100:clear")

    def test_a_range_with_a_sheet_prefix_is_used_as_is(self):
        transport = FakeTransport(
            ("values", 200, json_body({"spreadsheetId": "ss-1",
                                       "clearedRange": "todo!A2:B3"})))

        run_clear(transport, {"spreadsheet_id": "ss-1", "worksheet": "todo",
                              "range": "todo!A2:B3"})

        self.assertEqual(
            transport.calls[0]["url"],
            SHEETS_URL + "/ss-1/values/todo%21A2%3AB3:clear")

    def test_range_takes_a_template(self):
        transport = FakeTransport(
            ("values", 200, json_body({"spreadsheetId": "ss-1"})))

        run_clear(transport, {"spreadsheet_id": "ss-1", "worksheet": "todo",
                              "range": "{columns}"},
                    event={"data": {"columns": "A2:B9"}})

        self.assertEqual(
            transport.calls[0]["url"],
            SHEETS_URL + "/ss-1/values/todo%21A2%3AB9:clear")

    def test_requires_a_spreadsheet_id(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError):
            run_clear(transport, {"worksheet": "todo"})
        self.assertEqual(transport.calls, [])

    def test_errors_surface_the_api_message(self):
        transport = FakeTransport(
            ("values", 400,
             json_body({"error": {"message": "Unable to parse range"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_clear(transport, {"spreadsheet_id": "ss-1", "range": "???"})
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("Unable to parse range", str(caught.exception))


# --- trigger options: google-drive.folders / google-sheets.worksheets ---------------


class OptionsTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "body": body})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


def configure_connections(monkeypatch, connections):
    """A fake connections table, so options wrappers resolve their account."""
    tables = {"connections": OptionsTable(connections)}

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


class OptionsTable:
    """DynamoDB stand-in keyed by connection_id."""

    def __init__(self, items=None):
        self.items = dict(items or {})

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"].get("connection_id"))
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


GOOGLE_ACCOUNT = {"connection_id": "g", "provider": "google",
                  "status": "connected", "credential_id": "oauth#g"}


def install_google(monkeypatch, transport):
    from src.dapier.connections import discovery as provider

    monkeypatch.setattr(provider, "_default_transport", transport)
    monkeypatch.setattr(provider.tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))
    configure_connections(monkeypatch, {"g": dict(GOOGLE_ACCOUNT)})


def test_drive_folder_options_list_folders_only(monkeypatch):
    transport = OptionsTransport(
        ("drive/v3/files", 200, {"files": [
            {"id": "fold-1", "name": "Invoices",
             "mimeType": "application/vnd.google-apps.folder",
             "modified": "2026-09-28T09:00:00.000Z"}]}))
    install_google(monkeypatch, transport)

    status, payload = trigger_discovery.api_discover(
        {"connector": "google-drive", "kind": "options",
         "resource": "google-drive.folders", "connection_id": "g"})

    assert status == 200, payload
    assert payload["connector"] == "google-drive"
    assert payload["resource"] == "google-drive.folders"
    assert payload["options"] == [{"value": "fold-1", "label": "Invoices"}]
    assert payload["connection_id"] == "g"
    call, = transport.calls
    # the listing is the folder-only variant: the mimeType filter rides in q
    assert "mimeType%3D%27application%2Fvnd.google-apps.folder%27" in call["url"]
    assert "trashed%3Dfalse" in call["url"]


def test_both_drive_option_resources_are_in_the_catalog():
    catalog = trigger_discovery.trigger_discovery_catalog()

    assert set(catalog["options"]["google-drive"]) >= {
        "google-drive.files", "google-drive.folders"}


def test_worksheet_options_need_a_spreadsheet(monkeypatch):
    install_google(monkeypatch, OptionsTransport())

    status, payload = trigger_discovery.api_discover(
        {"connector": "google-sheets", "kind": "options",
         "resource": "google-sheets.worksheets", "connection_id": "g"})

    assert status == 404
    assert "spreadsheet_id" in payload["error"]


def test_worksheet_options_take_the_spreadsheet_from_the_event(monkeypatch):
    transport = OptionsTransport(
        ("fields=sheets.properties", 200, {"sheets": [
            {"properties": {"sheetId": 42, "title": "todo",
                            "gridProperties": {"rowCount": 100,
                                               "columnCount": 26}}},
            {"properties": {"sheetId": 43, "title": "done"}},
        ]}))
    install_google(monkeypatch, transport)

    status, payload = trigger_discovery.api_discover(
        {"connector": "google-sheets", "kind": "options",
         "resource": "google-sheets.worksheets", "event": "ss-4137",
         "connection_id": "g"})

    assert status == 200, payload
    assert payload["resource"] == "google-sheets.worksheets"
    # the value is the worksheet title — the sheets actions address tabs by name
    assert payload["options"] == [{"value": "todo", "label": "todo"},
                                  {"value": "done", "label": "done"}]
    call, = transport.calls
    assert call["url"].startswith(SHEETS_URL + "/ss-4137?")
    assert "fields=sheets.properties" in call["url"]


def test_both_sheets_option_resources_are_in_the_catalog():
    catalog = trigger_discovery.trigger_discovery_catalog()

    assert set(catalog["options"]["google-sheets"]) >= {
        "google-sheets.spreadsheets", "google-sheets.worksheets"}


# --- registry wiring ----------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_new_actions_are_registered_with_their_specs(self):
        specs = registry.action_specs()
        # drive_delete_file's optional set is being extended in-flight
        # (a ``permanent`` trash-vs-destroy flag), so pin required only.
        self.assertEqual(specs["drive_delete_file"][0],
                         {"connection_id", "file_id"})
        self.assertEqual(specs["drive_create_folder"],
                         ({"connection_id", "name"}, {"parent_folder_id"}))
        self.assertEqual(specs["sheets_clear_values"],
                         ({"connection_id", "spreadsheet_id"},
                          {"worksheet", "range"}))

    def test_find_file_keeps_its_field_spec(self):
        specs = registry.action_specs()
        self.assertEqual(specs["drive_find_file"],
                         ({"connection_id", "name"},
                          {"match", "folder"}))

    def test_a_chain_using_the_new_actions_validates(self):
        registry.validate_action_chain([
            {"type": "drive_find_file", "connection_id": "google",
             "name": "report"},
            {"type": "drive_delete_file", "connection_id": "google",
             "file_id": "{steps.find.output.file.id}"},
            {"type": "drive_create_folder", "connection_id": "google",
             "name": "{subject}", "parent_folder_id": "fold-1"},
            {"type": "sheets_clear_values", "connection_id": "google",
             "spreadsheet_id": "ss-1", "worksheet": "todo"},
        ])

    def test_validation_rejects_missing_and_unknown(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "drive_delete_file", "connection_id": "google"}])
        self.assertIn("missing: file_id", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "drive_create_folder", "connection_id": "google",
                 "name": "X", "parent_id": "fold-1"}])
        self.assertIn("unknown keys: parent_id", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "sheets_clear_values", "connection_id": "google",
                 "spreadsheet_id": "ss-1", "sheet_name": "todo"}])
        self.assertIn("unknown keys: sheet_name", str(caught.exception))

    def test_new_fields_carry_discover_and_help(self):
        def field(action_type, key):
            return next(field for field in registry.ACTIONS[action_type].fields
                        if field["key"] == key)

        self.assertEqual(field("drive_delete_file", "file_id")["discover"],
                         {"resource": "google-drive.files"})
        self.assertEqual(field("drive_create_folder",
                               "parent_folder_id")["discover"],
                         {"resource": "google-drive.folders"})
        self.assertIn("help", field("sheets_clear_values", "range"))


if __name__ == "__main__":
    unittest.main()
