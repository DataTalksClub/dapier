"""Action-staple tests for the YouTube/Sheets curation round:
youtube_remove_from_playlist, youtube_create_playlist, sheets_create_column.

Unit tests drive each registered runner with a fake provider transport and
the connection/token seams patched (the test_round_yts3zoom /
test_round_sheetstelegram pattern), asserting method, URL, request body and
the step-output shape. The registry half checks the new types' field specs,
save-time field typing, presence in registry.catalog(), and that
validate_action_chain accepts a valid chain and rejects missing required
fields, unknown keys and mistyped literals.
"""
import json
import unittest
from unittest.mock import patch

from src.dapier.connectors import registry
from plugins.google.runners.sheets import run_sheets_create_column
from plugins.google.runners.youtube import (
    run_youtube_create_playlist,
    run_youtube_remove_from_playlist,
)

import src.dapier.connectors  # noqa: F401  (import = registration)


YOUTUBE_CONNECTION = {"connection_id": "youtube", "provider": "youtube",
                      "status": "connected", "credential_id": "oauth#google"}
GOOGLE_CONNECTION = {"connection_id": "google", "provider": "google",
                     "status": "connected", "credential_id": "oauth#google"}

EVENT = {"connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"subject": "Deploying dapier", "playlist_id": "PL-9"}}


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


# --- youtube_remove_from_playlist -----------------------------------------------


def run_playlist_remove(transport, action, event=None, steps=None):
    action = {"type": "youtube_remove_from_playlist",
              "connection_id": "youtube", **action}
    with patch("plugins.google.runners.youtube._youtube_connection",
               return_value=dict(YOUTUBE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_youtube_remove_from_playlist(action, event or EVENT,
                                                steps=steps, transport=transport)


class YoutubeRemoveFromPlaylistTests(unittest.TestCase):
    def test_deletes_the_playlist_item_and_reports_removed(self):
        transport = FakeTransport(("playlistItems", 204, b""))

        output = run_playlist_remove(transport,
                                     {"playlist_item_id": "PI-1"})

        self.assertEqual(output, {"removed": True, "playlist_item_id": "PI-1"})
        call, = transport.calls
        self.assertEqual(call["method"], "DELETE")
        self.assertEqual(
            call["url"],
            "https://www.googleapis.com/youtube/v3/playlistItems?id=PI-1")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertIsNone(call["body"])

    def test_the_id_takes_a_template(self):
        transport = FakeTransport(("playlistItems", 204, b""))

        output = run_playlist_remove(
            transport,
            {"playlist_item_id": "{steps.add.output.playlist_item_id}"},
            steps={"add": {"output": {"playlist_item_id": "PI-7"}}})

        self.assertEqual(output["playlist_item_id"], "PI-7")
        self.assertIn("id=PI-7", transport.calls[0]["url"])

    def test_an_already_gone_item_is_removed_false_not_an_error(self):
        transport = FakeTransport(
            ("playlistItems", 404,
             json_body({"error": {"message": "The playlist item identified "
                                               "was not found"}})),
        )

        output = run_playlist_remove(transport, {"playlist_item_id": "PI-1"})

        self.assertEqual(output, {"removed": False,
                                  "playlist_item_id": "PI-1"})

    def test_other_errors_surface_the_api_message(self):
        transport = FakeTransport(
            ("playlistItems", 403,
             json_body({"error": {"message": "Insufficient permissions"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_playlist_remove(transport, {"playlist_item_id": "PI-1"})
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("Insufficient permissions", str(caught.exception))

    def test_requires_the_playlist_item_id(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError):
            run_playlist_remove(transport, {})
        self.assertEqual(transport.calls, [])


# --- youtube_create_playlist ----------------------------------------------------


def run_playlist_create(transport, action, event=None, steps=None):
    action = {"type": "youtube_create_playlist",
              "connection_id": "youtube", **action}
    with patch("plugins.google.runners.youtube._youtube_connection",
               return_value=dict(YOUTUBE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_youtube_create_playlist(action, event or EVENT,
                                           steps=steps, transport=transport)


class YoutubeCreatePlaylistTests(unittest.TestCase):
    def test_posts_the_snippet_and_status_and_returns_the_playlist(self):
        transport = FakeTransport(
            ("v3/playlists", 200,
             json_body({"id": "PL-9",
                        "snippet": {"title": "Deploying dapier"},
                        "status": {"privacyStatus": "public"}})))

        output = run_playlist_create(transport,
                                     {"title": "Deploying dapier",
                                      "description": "Every episode",
                                      "privacy_status": "public"})

        self.assertEqual(output, {
            "playlist_id": "PL-9", "title": "Deploying dapier",
            "url": "https://www.youtube.com/playlist?list=PL-9"})
        call, = transport.calls
        self.assertEqual(call["method"], "POST")
        self.assertEqual(
            call["url"],
            "https://www.googleapis.com/youtube/v3/playlists"
            "?part=snippet%2Cstatus")
        self.assertEqual(call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(call["headers"]["content-type"], "application/json")
        self.assertEqual(json.loads(call["body"]), {
            "snippet": {"title": "Deploying dapier",
                        "description": "Every episode"},
            "status": {"privacyStatus": "public"},
        })

    def test_privacy_defaults_to_private(self):
        transport = FakeTransport(
            ("v3/playlists", 200, json_body({"id": "PL-1"})))

        output = run_playlist_create(transport, {"title": "Standup clips"})

        self.assertEqual(json.loads(transport.calls[0]["body"]),
                         {"snippet": {"title": "Standup clips"},
                          "status": {"privacyStatus": "private"}})
        self.assertEqual(output["url"],
                         "https://www.youtube.com/playlist?list=PL-1")
        self.assertEqual(output["title"], "Standup clips")

    def test_title_takes_a_template(self):
        transport = FakeTransport(
            ("v3/playlists", 200, json_body({"id": "PL-2"})))

        run_playlist_create(transport, {"title": "{subject} clips"})

        self.assertEqual(
            json.loads(transport.calls[0]["body"])["snippet"],
            {"title": "Deploying dapier clips"})

    def test_unknown_privacy_status_rejected_before_any_call(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError) as caught:
            run_playlist_create(transport, {"title": "x",
                                            "privacy_status": "secret"})
        self.assertIn("privacy_status", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_requires_a_title(self):
        transport = FakeTransport()

        with self.assertRaises(ValueError):
            run_playlist_create(transport, {"privacy_status": "public"})
        self.assertEqual(transport.calls, [])


# --- sheets_create_column -------------------------------------------------------


def run_column_create(transport, action, event=None, steps=None):
    action = {"type": "sheets_create_column", "connection_id": "google",
              **action}
    with patch("plugins.google.runners.sheets._sheets_connection",
               return_value=dict(GOOGLE_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return run_sheets_create_column(action, event or EVENT, steps=steps,
                                        transport=transport)


def header_row(cells):
    return json_body({"values": [cells]} if cells else {})


class SheetsCreateColumnTests(unittest.TestCase):
    def test_appends_to_the_first_free_header_cell(self):
        transport = FakeTransport(
            ("%211%3A1", 200, header_row(["Date", "Task"])),
            ("%21C1", 200, json_body({})))

        output = run_column_create(transport, {"spreadsheet_id": "ss-1",
                                               "worksheet": "todo",
                                               "column": "Status"})

        self.assertEqual(output, {"spreadsheet_id": "ss-1", "worksheet": "todo",
                                  "column": "Status", "position": 3,
                                  "cell": "C1", "created": True})
        read_call, write_call = transport.calls
        self.assertEqual(read_call["method"], "GET")
        self.assertEqual(
            read_call["url"],
            "https://sheets.googleapis.com/v4/spreadsheets/ss-1"
            "/values/todo%211%3A1")
        self.assertEqual(write_call["method"], "PUT")
        self.assertEqual(
            write_call["url"],
            "https://sheets.googleapis.com/v4/spreadsheets/ss-1"
            "/values/todo%21C1?valueInputOption=USER_ENTERED")
        self.assertEqual(write_call["headers"]["authorization"], "Bearer tok")
        self.assertEqual(json.loads(write_call["body"]),
                         {"values": [["Status"]]})

    def test_an_existing_name_is_not_duplicated(self):
        transport = FakeTransport(
            ("%211%3A1", 200, header_row(["Date", "Task", "Status"])))

        output = run_column_create(transport, {"spreadsheet_id": "ss-1",
                                               "worksheet": "todo",
                                               "column": "status"})

        self.assertEqual(output, {"spreadsheet_id": "ss-1", "worksheet": "todo",
                                  "column": "Status", "position": 3,
                                  "cell": "C1", "created": False})
        self.assertEqual([call["method"] for call in transport.calls], ["GET"])

    def test_position_math_crosses_the_26_column_boundary(self):
        transport = FakeTransport(
            ("%211%3A1", 200,
             header_row([f"Col {number}" for number in range(1, 27)])),
            ("%21AA1", 200, json_body({})))

        output = run_column_create(transport, {"spreadsheet_id": "ss-1",
                                               "column": "Notes"})

        self.assertEqual(output["position"], 27)
        self.assertEqual(output["cell"], "AA1")
        self.assertIn("/values/Sheet1%21AA1?", transport.calls[1]["url"])

    def test_fills_a_gap_in_the_header_row(self):
        transport = FakeTransport(
            ("%211%3A1", 200, header_row(["Date", "", "Status"])),
            ("%21B1", 200, json_body({})))

        output = run_column_create(transport, {"spreadsheet_id": "ss-1",
                                               "worksheet": "todo",
                                               "column": "Task"})

        self.assertEqual(output["position"], 2)
        self.assertEqual(output["cell"], "B1")
        self.assertEqual(json.loads(transport.calls[1]["body"]),
                         {"values": [["Task"]]})

    def test_an_empty_header_row_starts_at_a1(self):
        transport = FakeTransport(
            ("%211%3A1", 200, header_row([])),
            ("%21A1", 200, json_body({})))

        output = run_column_create(transport, {"spreadsheet_id": "ss-1",
                                               "worksheet": "todo",
                                               "column": "Date"})

        self.assertEqual(output["position"], 1)
        self.assertEqual(output["cell"], "A1")
        self.assertEqual(output["created"], True)

    def test_a_failed_header_read_stops_before_the_write(self):
        transport = FakeTransport(
            ("%211%3A1", 403,
             json_body({"error": {"message": "The caller does not have "
                                              "permission"}})))

        with self.assertRaises(RuntimeError) as caught:
            run_column_create(transport, {"spreadsheet_id": "ss-1",
                                          "column": "Status"})
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertEqual(len(transport.calls), 1)

    def test_requires_spreadsheet_and_column(self):
        transport = FakeTransport()

        for action in ({"column": "Status"}, {"spreadsheet_id": "ss-1"}):
            with self.assertRaises(ValueError):
                run_column_create(transport, action)
        self.assertEqual(transport.calls, [])


# --- registry wiring ------------------------------------------------------------


class RegistryTests(unittest.TestCase):
    def test_new_actions_are_registered_with_their_specs(self):
        specs = registry.action_specs()
        self.assertEqual(specs["youtube_remove_from_playlist"],
                         ({"connection_id", "playlist_item_id"}, frozenset()))
        self.assertEqual(specs["youtube_create_playlist"],
                         ({"connection_id", "title"},
                          {"description", "privacy_status"}))
        self.assertEqual(specs["sheets_create_column"],
                         ({"connection_id", "spreadsheet_id", "column"},
                          {"worksheet"}))

    def test_new_actions_reach_the_catalog(self):
        catalog = registry.catalog()
        types = {entry["type"] for entry in catalog["actions"]}
        for type_ in ("youtube_remove_from_playlist",
                      "youtube_create_playlist", "sheets_create_column"):
            self.assertIn(type_, types)

    def test_save_time_field_typing(self):
        def field(type_, key):
            return next(field for field in registry.ACTIONS[type_].fields
                        if field["key"] == key)
        privacy = field("youtube_create_playlist", "privacy_status")
        self.assertEqual(privacy["type"], "select")
        self.assertEqual(privacy["options"],
                         ["private", "public", "unlisted"])
        self.assertEqual(privacy["default"], "private")
        self.assertEqual(
            field("sheets_create_column", "spreadsheet_id")["discover"],
            {"resource": "google-sheets.spreadsheets"})
        self.assertEqual(
            field("sheets_create_column", "worksheet")["discover"],
            {"resource": "google-sheets.worksheets",
             "params": {"spreadsheet_id": "spreadsheet_id"}})
        self.assertEqual(
            field("sheets_create_column", "column")["discover"],
            {"resource": "google-sheets.columns",
             "params": {"spreadsheet_id": "spreadsheet_id",
                        "worksheet": "worksheet"}})

    def test_a_chain_using_the_new_actions_validates(self):
        registry.validate_action_chain([
            {"type": "youtube_create_playlist", "connection_id": "youtube",
             "title": "{subject} clips"},
            {"type": "youtube_add_to_playlist", "connection_id": "youtube",
             "playlist_id": "{steps.p.output.playlist_id}",
             "video_id": "{trigger.video_id}"},
            {"type": "youtube_remove_from_playlist", "connection_id": "youtube",
             "playlist_item_id": "{steps.add.output.playlist_item_id}"},
            {"type": "sheets_create_column", "connection_id": "google",
             "spreadsheet_id": "ss-1", "worksheet": "todo",
             "column": "Status"},
        ])

    def test_validation_rejects_missing_and_unknown_and_mistyped(self):
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "youtube_remove_from_playlist",
                 "connection_id": "youtube"}])
        self.assertIn("missing: playlist_item_id", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "youtube_create_playlist", "connection_id": "youtube",
                 "title": "x", "video_id": "nope"}])
        self.assertIn("unknown keys: video_id", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "youtube_create_playlist", "connection_id": "youtube",
                 "title": "x", "privacy_status": "secret"}])
        self.assertIn("privacy_status", str(caught.exception))
        with self.assertRaises(registry.ActionError) as caught:
            registry.validate_action_chain([
                {"type": "sheets_create_column", "connection_id": "google",
                 "spreadsheet_id": "ss-1"}])
        self.assertIn("missing: column", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
