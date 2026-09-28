"""Poll presets: the Sheets "New Spreadsheet Row" and Drive "New File in
Folder" sources as first-class poll-trigger choices behind their palette
chips (docs/connector-coverage-audit.md gap 2).

Covers the save-time shape (source validate → build_item), the fetch
transform (values.get / files.list through the shared provider helpers,
header-row skip, watermark seeding on the first fire), the end-to-end fire
with seen-store dedupe, and the chips' sample pulls. Google calls run
through a fake transport, the seen store and cursors through fake DynamoDB
tables — no network, no moto.
"""
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.dapier.connections import discovery
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

ACTIONS = [{"type": "email_send", "to": "dest@example.test"}]

SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets/"
DRIVE_URL = "https://www.googleapis.com/drive/v3/files"


class FakeCursorTable:
    """DynamoDB stand-in keyed by cursor_id (poll cursors) or scope_id
    (the seen store's per-trigger sets, triggers.seen)."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _partition(key_or_item):
        return (key_or_item.get("cursor_id")
                if "cursor_id" in key_or_item else key_or_item.get("scope_id"))

    def get_item(self, Key):
        item = self.items.get(self._partition(Key))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[self._partition(Item)] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(self._partition(Key), None)


def build(source, name, **body):
    body.setdefault("actions", [dict(ACTIONS[0])])
    body["source"] = source
    body["expression"] = "rate(5 minutes)"
    return poll_triggers.build_item({"name": name, **body}, "op@example.test")


def source_fetch(source_name, item, cursor, transport):
    """One direct source fetch with the connection's token refresh stubbed —
    the refresh itself is poll_triggers' job (tested there), not duplicated."""
    with patch.object(poll_triggers, "_bearer_token", return_value="fresh-token"):
        return poll_sources.SOURCES[source_name].fetch(
            item, cursor, transport=transport)


def sheet_page(*rows):
    """A values.get body: row 1 is the header, the rest are data rows."""
    return json.dumps({"values": [["Task", "Status"], *list(rows)]}).encode()


def sheets_transport(recorder, rows):
    def transport(method, url, headers=None, body=None, timeout=None):
        recorder.append({"method": method, "url": url, "headers": headers})
        assert method == "GET"
        assert url.startswith(SHEETS_URL)
        assert headers.get("authorization") == "Bearer fresh-token"
        return 200, sheet_page(*rows)
    return transport


def run_fire(item, transport, *, cursors=None, fail_on=None):
    """One fire() against ``transport`` with real cursor/seen machinery."""
    fired_events = []
    cursors = cursors or FakeCursorTable()

    def fake_execute(event, **_kwargs):
        if fail_on is not None and event["data"]["item_id"] == fail_on:
            raise RuntimeError("downstream exploded")
        fired_events.append(event)

    with patch.object(poll_triggers, "get_item", return_value=item), \
         patch.object(poll_triggers, "_bearer_token", return_value="fresh-token"), \
         patch.object(discovery, "_default_transport", side_effect=transport), \
         patch("src.dapier.engine.execute", side_effect=fake_execute), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)

    return result, fired_events, cursors


# --- registration shape ---------------------------------------------------------


class TestRegistrationShape:
    def test_the_provider_sources_are_registered(self):
        assert poll_sources.source_names() == [
            "dropbox.files", "google-calendar.events", "google-drive.deletions",
            "google-drive.files", "google-drive.updates", "google-sheets.rows",
            "mailchimp.members", "rss", "s3", "s3.deletions", "s3.updates",
            "slack.messages", "youtube.videos", "zoom.recordings"]

    def test_each_source_matches_its_palette_chip(self):
        from src.dapier.connectors import registry

        for name in ("google-sheets.rows", "google-drive.files", "zoom.recordings"):
            source = poll_sources.resolve(name)
            chip = registry.CONNECTORS[source.connector]
            assert source.event in chip.events
            assert source.label == chip.label


# --- save-time shape --------------------------------------------------------------


class TestSheetsSave:
    def test_a_sheets_poll_builds_a_seen_store_poll(self):
        item = build("google-sheets.rows", "sheets-watch",
                     spreadsheet_id="SS-1", worksheet="todo",
                     connection_id="google")

        assert item["source"] == "google-sheets.rows"
        assert item["spreadsheet_id"] == "SS-1"
        assert item["worksheet"] == "todo"
        assert item["connection_id"] == "google"
        assert item["cursor_mode"] == "next_cursor"  # seen-store dedupe
        assert item["url"] == ""  # the source fetches; no HTTP url

    def test_worksheet_is_optional_and_defaults_at_fetch_time(self):
        item = build("google-sheets.rows", "sheets-watch",
                     spreadsheet_id="SS-1", connection_id="google")

        assert item["worksheet"] == ""

    def test_spreadsheet_id_is_required(self):
        with pytest.raises(TriggerError, match="spreadsheet_id"):
            build("google-sheets.rows", "sheets-watch", connection_id="google")

    def test_the_connection_is_required(self):
        with pytest.raises(TriggerError, match="connection_id"):
            build("google-sheets.rows", "sheets-watch", spreadsheet_id="SS-1")

    def test_the_view_shows_the_watched_sheet(self):
        view = poll_triggers.public_view(
            build("google-sheets.rows", "sheets-watch",
                  spreadsheet_id="SS-1", worksheet="todo",
                  connection_id="google"))

        assert view["source"] == "google-sheets.rows"
        assert view["spreadsheet_id"] == "SS-1"
        assert view["worksheet"] == "todo"


class TestDriveSave:
    def test_a_drive_poll_builds_a_seen_store_poll(self):
        item = build("google-drive.files", "drive-watch",
                     folder_id="FLD-9", connection_id="google")

        assert item["source"] == "google-drive.files"
        assert item["folder_id"] == "FLD-9"
        assert item["connection_id"] == "google"
        assert item["cursor_mode"] == "next_cursor"
        assert item["url"] == ""

    def test_the_folder_is_required(self):
        with pytest.raises(TriggerError, match="folder_id"):
            build("google-drive.files", "drive-watch", connection_id="google")

    def test_the_connection_is_required(self):
        with pytest.raises(TriggerError, match="connection_id"):
            build("google-drive.files", "drive-watch", folder_id="FLD-9")

    def test_the_view_shows_the_watched_folder(self):
        view = poll_triggers.public_view(
            build("google-drive.files", "drive-watch",
                  folder_id="FLD-9", connection_id="google"))

        assert view["source"] == "google-drive.files"
        assert view["folder_id"] == "FLD-9"


# --- the drive fetch -------------------------------------------------------------


def drive_transport(recorder, files):
    def transport(method, url, headers=None, body=None, timeout=None):
        recorder.append({"method": method, "url": url, "headers": headers})
        assert method == "GET"
        assert url.startswith(DRIVE_URL)
        assert headers.get("authorization") == "Bearer fresh-token"
        return 200, json.dumps({"files": list(files)}).encode()
    return transport


def drive_file(file_id, name, created, modified=None):
    return {"id": file_id, "name": name, "mimeType": "application/pdf",
            "createdTime": created,
            "modifiedTime": modified or created,
            "size": "81244",
            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}


class TestDriveFetch:
    def item(self, **overrides):
        return build("google-drive.files", "drive-watch",
                     folder_id="FLD-9", connection_id="google", **overrides)

    def test_files_list_watches_the_folder_newest_first(self):
        calls = []
        file_one = drive_file("f-1", "report.pdf", "2026-09-28T02:00:00.000Z")
        items, next_cursor = source_fetch(
            "google-drive.files", self.item(), "2026-09-28T01:00:00.000Z",
            drive_transport(calls, [file_one]))

        assert len(calls) == 1
        query = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0]["url"]).query)
        assert query["q"] == ["'FLD-9' in parents and trashed=false"]
        assert query["orderBy"] == ["createdTime desc"]
        assert "createdTime" in query["fields"][0]
        # Items are the API's own file objects, JSON-safe as returned.
        assert items == [file_one]
        assert next_cursor == "2026-09-28T02:00:00.000Z"

    def test_the_first_fetch_seeds_at_now_without_items(self):
        items, seed = source_fetch(
            "google-drive.files", self.item(), None,
            drive_transport([], [
                drive_file("f-1", "old.pdf", "2026-09-28T01:00:00.000Z")]))

        assert items == []  # the folder's existing files are history, not news
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", seed)

    def test_only_files_created_after_the_watermark_are_fresh(self):
        items, next_cursor = source_fetch(
            "google-drive.files", self.item(), "2026-09-28T02:00:00.000Z",
            drive_transport([], [
                drive_file("f-3", "newest.pdf", "2026-09-28T03:00:00.000Z"),
                drive_file("f-2", "newer.pdf", "2026-09-28T02:00:00.000Z"),
            ]))

        assert [file["id"] for file in items] == ["f-3"]  # oldest first
        assert next_cursor == "2026-09-28T03:00:00.000Z"

    def test_an_edited_old_file_is_never_fresh(self):
        """createdTime keys the watermark: an edit bumps modifiedTime, not
        the creation the trigger watches."""
        items, _ = source_fetch(
            "google-drive.files", self.item(), "2026-09-28T02:00:00.000Z",
            drive_transport([], [
                drive_file("f-1", "report.pdf", "2026-09-28T01:00:00.000Z",
                           modified="2026-09-28T05:00:00.000Z")]))

        assert items == []

    def test_a_missing_folder_fails_the_fetch(self):
        item = self.item()
        del item["folder_id"]

        with pytest.raises(RuntimeError, match="folder_id"):
            source_fetch("google-drive.files", item, None, drive_transport([], []))

    def test_a_missing_connection_fails_the_fetch(self):
        item = self.item()
        del item["connection_id"]

        with pytest.raises(RuntimeError, match="connection_id"):
            source_fetch("google-drive.files", item, None, drive_transport([], []))

    def test_a_provider_error_is_a_runtime_error(self):
        def transport(method, url, headers=None, body=None, timeout=None):
            return 403, b'{"error": {"message": "insufficient permissions"}}'

        with pytest.raises(RuntimeError, match="drive poll failed"):
            source_fetch("google-drive.files", self.item(),
                         "2026-09-28T02:00:00.000Z", transport)


class TestDriveFire:
    def item(self):
        return build("google-drive.files", "drive-watch",
                     folder_id="FLD-9", connection_id="google")

    def drive_now(self, offset_seconds=0):
        moment = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def test_the_first_fire_seeds_and_fires_nothing(self):
        result, fired, cursors = run_fire(
            self.item(), drive_transport([], [
                drive_file("f-1", "report.pdf", "2026-09-28T02:00:00.000Z")]))

        assert result == {"poll": "drive-watch", "fired": 0}
        assert fired == []
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z",
                            cursors.items["poll#drive-watch"]["cursor"])

    def test_a_new_file_fires_as_a_drive_event(self):
        cursors = FakeCursorTable()
        run_fire(self.item(), drive_transport([], [
            drive_file("f-1", "report.pdf", "2026-09-28T02:00:00.000Z")]),
            cursors=cursors)

        created_after_the_seed = self.drive_now(5)
        result, fired, _ = run_fire(
            self.item(), drive_transport([], [
                drive_file("f-1", "report.pdf", "2026-09-28T02:00:00.000Z"),
                drive_file("f-2", "invoice.pdf", created_after_the_seed)]),
            cursors=cursors)

        assert result == {"poll": "drive-watch", "fired": 1}
        event = fired[0]
        assert event["connector"] == "google-drive"
        assert event["event"] == "file.created"
        assert event["data"]["poll"] == "drive-watch"
        assert event["data"]["name"] == "invoice.pdf"

    def test_the_same_file_does_not_fire_twice(self):
        """A re-listed file rides the same createdTime: never fresh again —
        and the seen store is the backstop for an equal-timestamp re-list."""
        created_after_the_seed = self.drive_now(5)
        cursors = FakeCursorTable()
        run_fire(self.item(), drive_transport([], [
            drive_file("f-1", "report.pdf", "2026-09-28T02:00:00.000Z")]),
            cursors=cursors)
        first, fired_first, _ = run_fire(
            self.item(), drive_transport([], [
                drive_file("f-1", "report.pdf", "2026-09-28T02:00:00.000Z"),
                drive_file("f-2", "invoice.pdf", created_after_the_seed)]),
            cursors=cursors)
        assert first["fired"] == 1

        second, fired_second, _ = run_fire(
            self.item(), drive_transport([], [
                drive_file("f-1", "report.pdf", "2026-09-28T02:00:00.000Z"),
                drive_file("f-2", "invoice.pdf", created_after_the_seed,
                           modified=self.drive_now(60))]),
            cursors=cursors)

        assert second["fired"] == 0
        assert fired_second == []


# --- the chips' sample pulls -------------------------------------------------------


class TestSamplePull:
    def test_sheets_sample_falls_back_to_a_synthetic_row(self, monkeypatch):
        from src.dapier.connectors import trigger_discovery

        monkeypatch.setattr(trigger_discovery, "history_sample",
                            lambda connector, event=None: None)
        result = trigger_discovery.discover("google-sheets")

        assert result["event"] == "row.new"
        assert result["source"] == "synthetic"
        assert result["sample"]["data"]["Invoice"]

    def test_drive_sample_falls_back_to_a_synthetic_file(self, monkeypatch):
        from src.dapier.connectors import trigger_discovery

        monkeypatch.setattr(trigger_discovery, "history_sample",
                            lambda connector, event=None: None)
        result = trigger_discovery.discover("google-drive")

        assert result["event"] == "file.created"
        assert result["source"] == "synthetic"
        assert result["sample"]["data"]["name"]

    def test_a_stored_sheets_poll_pulls_a_live_row(self, monkeypatch):
        from src.dapier.connectors import trigger_discovery

        item = build("google-sheets.rows", "sheets-watch",
                     spreadsheet_id="SS-1", worksheet="todo",
                     connection_id="google")
        monkeypatch.setattr(poll_triggers, "get_item",
                            lambda name, table_ref=None: item)
        monkeypatch.setattr(poll_triggers, "_bearer_token",
                            lambda connection_id: "fresh-token")
        monkeypatch.setattr(discovery, "_default_transport",
                            sheets_transport([], [("ship", "DONE")]))

        result = trigger_discovery.discover("google-sheets", event="sheets-watch")

        assert result["source"] == "live"
        assert result["sample"]["event"] == "row.new"
        assert result["sample"]["data"]["poll"] == "sheets-watch"
        assert result["sample"]["data"]["Task"] == "ship"
        assert result["connection_id"] == "google"


if __name__ == "__main__":
    pytest.main([__file__])
