"""The Google trigger chips as real poll sources: google-sheets.rows,
google-sheets.updates and google-drive.files.

Zapier's "New Spreadsheet Row" / "New or Updated Row" / "New File in
Folder": a stored poll trigger with the chip's source reads the provider on
its schedule (OAuth token refreshed through poll_triggers._bearer_token) and
publishes the chip's connector/event, scoped per trigger through the
poll-name filter. Covered here: save-time validation, the seeded first fire
(enabling must not fire everything already there), strictly-new subsequent
fetches, end-to-end fires on fake tables, and the chips' sample pulls (live
/ history / synthetic). Transport is stubbed at the shared provider seam,
the same way tests/test_trigger_samples.py stubs Google listings.
"""
import json
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.dapier.connectors import drive, sheets  # noqa: F401  (import = registration)
from src.dapier.connectors import trigger_discovery
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError
import src.dapier.connections.discovery as provider


# --- fakes -----------------------------------------------------------------------


class Transport:
    """One canned provider response, recording every call."""

    def __init__(self, payload, status=200, pages=None):
        self.payload = payload
        self.status = status
        self.pages = pages or []  # optional per-call payloads (pagination)
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers})
        payload = self.pages.pop(0) if self.pages else self.payload
        return self.status, json.dumps(payload).encode()


class FakePollTable:
    """DynamoDB stand-in keyed by poll_id."""

    def __init__(self):
        self.items = {}

    def scan(self, **_kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}

    def get_item(self, Key):
        item = self.items.get(Key["poll_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["poll_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["poll_id"], None)


class FakeCursorTable:
    """DynamoDB stand-in keyed by cursor_id (poll cursors) or scope_id
    (the seen store's per-trigger sets)."""

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


class FakeEvents:
    """EventBridge stand-in."""

    def put_rule(self, **_kwargs):
        pass

    def put_targets(self, **_kwargs):
        pass

    def remove_targets(self, Rule, Ids, **_kwargs):
        pass

    def delete_rule(self, Name, **_kwargs):
        pass


def sheets_body(**overrides):
    body = {
        "name": "invoices-rows",
        "expression": "rate(1 hour)",
        "source": "google-sheets.rows",
        "spreadsheet_id": "ss-4137",
        "worksheet": "Invoices",
        "connection_id": "google",
        "actions": [{"type": "email_send", "to": "billing@example.test"}],
    }
    body.update(overrides)
    return body


def drive_body(**overrides):
    body = {
        "name": "folder-files",
        "expression": "rate(1 hour)",
        "source": "google-drive.files",
        "folder_id": "fold-4137",
        "connection_id": "google",
        "actions": [{"type": "email_send", "to": "billing@example.test"}],
    }
    body.update(overrides)
    return body


def sheet_values(*data_rows):
    """A values.get page: the header row plus the data rows."""
    return {"values": [["Date", "Invoice", "Amount"], *data_rows]}


def drive_page(*created_times):
    """A files.list page, one PDF per given createdTime."""
    return {"files": [
        {"id": f"file-{index}", "name": f"file-{index}.pdf",
         "mimeType": "application/pdf", "createdTime": created,
         "modifiedTime": created, "size": "81244",
         "webViewLink": f"https://drive.google.com/file/d/file-{index}/view"}
        for index, created in enumerate(created_times)]}


@pytest.fixture
def google_transport(monkeypatch):
    """Install a canned transport at the shared provider seam and pin the
    OAuth token refresh; returns the installed transport."""
    def install(transport):
        monkeypatch.setattr(provider, "_default_transport", transport)
        monkeypatch.setattr(poll_triggers, "_bearer_token",
                            lambda connection_id: "fresh-token")
        return transport
    return install


def stored(body):
    """The stored item build_item would persist for ``body``."""
    return poll_triggers.build_item(body, "op@example.test")


# --- registration -----------------------------------------------------------------


def test_both_chips_register_a_poll_source():
    sheet_source = poll_sources.SOURCES["google-sheets.rows"]
    drive_source = poll_sources.SOURCES["google-drive.files"]
    assert (sheet_source.connector, sheet_source.event) == ("google-sheets", "row.new")
    assert (drive_source.connector, drive_source.event) == ("google-drive", "file.created")
    assert set(poll_sources.source_names()) >= {"google-sheets.rows", "google-drive.files"}


# --- save validation --------------------------------------------------------------


def test_sheets_save_stores_the_fetch_spec():
    item = stored(sheets_body())

    assert item["source"] == "google-sheets.rows"
    assert item["spreadsheet_id"] == "ss-4137"
    assert item["worksheet"] == "Invoices"
    assert item["connection_id"] == "google"
    assert item["cursor_mode"] == "next_cursor"
    assert item["url"] == ""


def test_sheets_save_defaults_the_worksheet_to_the_first_sheet():
    item = stored(sheets_body(worksheet=None))

    assert item["worksheet"] == ""


@pytest.mark.parametrize("missing", ["spreadsheet_id", "connection_id"])
def test_sheets_save_requires_its_params(missing):
    with pytest.raises(TriggerError, match=missing):
        stored(sheets_body(**{missing: ""}))


def test_drive_save_stores_the_fetch_spec():
    item = stored(drive_body())

    assert item["source"] == "google-drive.files"
    assert item["folder_id"] == "fold-4137"
    assert item["connection_id"] == "google"
    assert item["cursor_mode"] == "next_cursor"
    assert item["id_path"] == "id"
    assert item["url"] == ""


@pytest.mark.parametrize("missing", ["folder_id", "connection_id"])
def test_drive_save_requires_its_params(missing):
    with pytest.raises(TriggerError, match=missing):
        stored(drive_body(**{missing: ""}))


# --- sheets fetch: seed, then strictly-new rows ------------------------------------


def test_sheets_first_fetch_seeds_without_emitting(google_transport):
    transport = google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"])))
    item = stored(sheets_body())

    items, next_cursor = sheets._sheets_poll_fetch(item, None)

    assert items == []
    assert next_cursor == "3"  # the worksheet's last existing row
    assert transport.calls[0]["url"].startswith(
        "https://sheets.googleapis.com/v4/spreadsheets/ss-4137/values/")
    assert "Invoices%21" in transport.calls[0]["url"]  # the quoted range
    assert transport.calls[0]["headers"]["authorization"] == "Bearer fresh-token"


def test_sheets_empty_sheet_seeds_at_the_header(google_transport):
    google_transport(Transport(sheet_values()))
    item = stored(sheets_body())

    items, next_cursor = sheets._sheets_poll_fetch(item, None)

    assert items == []
    assert next_cursor == "1"


def test_sheets_next_fetch_returns_only_new_rows_oldest_first(google_transport):
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"],
        ["2026-09-28", "INV-3", "27.00"],
        ["2026-09-29", "INV-4", "36.00"])))
    item = stored(sheets_body())

    items, next_cursor = sheets._sheets_poll_fetch(item, "3")

    assert [entry["row"] for entry in items] == [4, 5]
    assert next_cursor == "5"


def test_sheets_items_name_columns_and_carry_the_row_twice(google_transport):
    google_transport(Transport(sheet_values(
        ["2026-09-26"],
        ["2026-09-28", "INV-2", "18.00", "extra"])))
    item = stored(sheets_body())

    items, _next = sheets._sheets_poll_fetch(item, "1")

    # short rows read "" under the missing headers; trailing cells without a
    # header are dropped; the row number sits under both keys
    assert items[0] == {"row": 2, "id": "2", "Date": "2026-09-26",
                        "Invoice": "", "Amount": ""}
    assert items[1] == {"row": 3, "id": "3", "Date": "2026-09-28",
                        "Invoice": "INV-2", "Amount": "18.00"}


def test_sheets_nothing_new_keeps_the_cursor(google_transport):
    google_transport(Transport(sheet_values(["2026-09-26", "INV-1", "9.00"])))
    item = stored(sheets_body())

    items, next_cursor = sheets._sheets_poll_fetch(item, "2")

    assert items == []
    assert next_cursor == "2"


def test_sheets_failed_fetch_raises_runtimeerror(google_transport):
    google_transport(Transport({"error": "boom"}, status=500))
    item = stored(sheets_body())

    with pytest.raises(RuntimeError, match="HTTP 500"):
        sheets._sheets_poll_fetch(item, "2")


# --- sheets updates fetch: a seeded diff over the worksheet's cells -----------------


def updates_body(**overrides):
    body = sheets_body(source="google-sheets.updates")
    body["name"] = "invoices-edits"
    body.update(overrides)
    return body


def test_updates_source_registers_for_the_row_updated_event():
    source = poll_sources.SOURCES["google-sheets.updates"]

    assert (source.connector, source.event) == ("google-sheets", "row.updated")
    assert set(poll_sources.source_names()) >= {"google-sheets.rows",
                                                "google-sheets.updates"}


def test_updates_save_stores_the_rows_spec_with_a_digest_id_path():
    item = stored(updates_body())

    assert item["source"] == "google-sheets.updates"
    assert item["spreadsheet_id"] == "ss-4137"
    assert item["worksheet"] == "Invoices"
    assert item["connection_id"] == "google"
    assert item["cursor_mode"] == "next_cursor"
    # an updated row keeps its row number, so the seen-set must key on the
    # digest-carrying id, not the row number
    assert item["id_path"] == "id"
    assert item["url"] == ""


@pytest.mark.parametrize("missing", ["spreadsheet_id", "connection_id"])
def test_updates_save_requires_its_params(missing):
    with pytest.raises(TriggerError, match=missing):
        stored(updates_body(**{missing: ""}))


def test_updates_first_fetch_seeds_the_snapshot_without_emitting(google_transport):
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"])))
    item = stored(updates_body())

    items, next_cursor = sheets._sheets_row_updated_fetch(item, None)

    assert items == []
    snapshot = json.loads(next_cursor)
    assert snapshot["v"] == 1
    assert sorted(snapshot["rows"]) == ["2", "3"]


def test_updates_fetch_emits_an_edited_row_once(google_transport):
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"])))
    item = stored(updates_body())

    _items, seed = sheets._sheets_row_updated_fetch(item, None)
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "99.00"])))

    items, next_cursor = sheets._sheets_row_updated_fetch(item, seed)

    assert len(items) == 1
    edited = items[0]
    assert edited["row"] == 3
    assert edited["id"].startswith("3:")  # the digest-carrying event id
    assert edited["Invoice"] == "INV-2"
    assert edited["Amount"] == "99.00"
    assert json.loads(next_cursor)["rows"]["3"] != json.loads(seed)["rows"]["3"]

    # the edit is news exactly once: the parked snapshot recognizes it
    again, _cursor = sheets._sheets_row_updated_fetch(item, next_cursor)
    assert again == []


def test_updates_fetch_skips_new_and_unchanged_rows(google_transport):
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"])))
    item = stored(updates_body())

    _items, seed = sheets._sheets_row_updated_fetch(item, None)
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"],
        ["2026-09-29", "INV-4", "36.00"])))  # a brand-new row only

    items, next_cursor = sheets._sheets_row_updated_fetch(item, seed)

    assert items == []  # creations are row.new's news, not double-fired here
    assert json.loads(next_cursor)["rows"] == {
        "2": json.loads(seed)["rows"]["2"],
        "3": json.loads(seed)["rows"]["3"],
        "4": json.loads(next_cursor)["rows"]["4"],
    }


def test_updates_foreign_cursor_reseeds_without_emitting(google_transport):
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"])))
    item = stored(updates_body())

    items, next_cursor = sheets._sheets_row_updated_fetch(item, "3")

    assert items == []  # a rows-source cursor is no diff baseline
    assert json.loads(next_cursor)["rows"]


def test_updates_fetch_reseeds_when_the_watch_moves(google_transport):
    google_transport(Transport(sheet_values(["2026-09-26", "INV-1", "9.00"])))
    item = stored(updates_body())

    _items, seed = sheets._sheets_row_updated_fetch(item, None)
    google_transport(Transport(sheet_values(["2026-09-26", "INV-1", "9.00"])))
    moved = stored(updates_body(worksheet="Archive"))
    items, next_cursor = sheets._sheets_row_updated_fetch(moved, seed)

    assert items == []
    assert json.loads(next_cursor)["worksheet"] == "Archive"


def test_updates_failed_fetch_raises_runtimeerror(google_transport):
    google_transport(Transport({"error": "boom"}, status=500))
    item = stored(updates_body())

    with pytest.raises(RuntimeError, match="HTTP 500"):
        sheets._sheets_row_updated_fetch(item, json.dumps(
            {"v": 1, "spreadsheet_id": "ss-4137", "worksheet": "Invoices",
             "rows": {"2": "x"}}))


# --- drive fetch: seed at now, then strictly-newer files ---------------------------


def test_drive_first_fetch_seeds_at_now_without_emitting(google_transport):
    transport = google_transport(Transport(drive_page(
        "2026-09-26T10:00:00.000Z", "2026-09-27T10:00:00.000Z")))
    item = stored(drive_body())

    files, next_cursor = drive._drive_poll_fetch(item, None)

    assert files == []
    seeded = datetime.fromisoformat(next_cursor.replace("Z", "+00:00"))
    assert seeded.tzinfo is not None and seeded > datetime(2026, 1, 1, tzinfo=timezone.utc)
    url = transport.calls[0]["url"]
    assert url.startswith("https://www.googleapis.com/drive/v3/files?")
    assert "%27fold-4137%27+in+parents" in url
    assert "orderBy=createdTime+desc" in url
    assert "pageSize=100" in url
    assert "createdTime%2CmodifiedTime%2Csize%2CwebViewLink" in url
    assert transport.calls[0]["headers"]["authorization"] == "Bearer fresh-token"


def test_drive_next_fetch_returns_only_new_files_oldest_first(google_transport):
    google_transport(Transport(drive_page(
        "2026-09-26T10:00:00.000Z",
        "2026-09-28T10:00:00.000Z",
        "2026-09-27T10:00:00.000Z")))
    item = stored(drive_body())

    files, next_cursor = drive._drive_poll_fetch(item, "2026-09-26T10:00:00.000Z")

    assert [entry["id"] for entry in files] == ["file-2", "file-1"]  # oldest first
    assert next_cursor == "2026-09-28T10:00:00.000Z"  # the newest fired createdTime


def test_drive_nothing_new_keeps_the_cursor(google_transport):
    google_transport(Transport(drive_page("2026-09-26T10:00:00.000Z")))
    item = stored(drive_body())

    files, next_cursor = drive._drive_poll_fetch(item, "2026-09-28T10:00:00.000Z")

    assert files == []
    assert next_cursor == "2026-09-28T10:00:00.000Z"


def test_drive_follows_the_page_token(google_transport):
    transport = google_transport(Transport(
        drive_page("2026-09-26T10:00:00.000Z"),
        pages=[{"files": drive_page("2026-09-28T10:00:00.000Z")["files"],
                "nextPageToken": "page-2"},
               {"files": drive_page("2026-09-27T10:00:00.000Z")["files"],
                "nextPageToken": "page-3"}]))
    item = stored(drive_body())

    files, next_cursor = drive._drive_poll_fetch(item, "2026-09-25T00:00:00.000Z")

    # all three pages merged, oldest first for firing
    assert [entry["createdTime"] for entry in files] == [
        "2026-09-26T10:00:00.000Z", "2026-09-27T10:00:00.000Z",
        "2026-09-28T10:00:00.000Z"]
    assert next_cursor == "2026-09-28T10:00:00.000Z"
    assert "pageToken=page-2" in transport.calls[1]["url"]


def test_drive_failed_fetch_raises_runtimeerror(google_transport):
    google_transport(Transport({"error": "boom"}, status=502))
    item = stored(drive_body())

    with pytest.raises(RuntimeError, match="HTTP 502"):
        drive._drive_poll_fetch(item, "2026-09-26T10:00:00.000Z")


# --- end-to-end fires on fake tables ------------------------------------------------


def saved_trigger(body):
    polls, cursors, events = FakePollTable(), FakeCursorTable(), FakeEvents()
    poll_triggers.api_save(body, "op@example.test", table_ref=polls,
                           cursor_table_ref=cursors, events_client=events,
                           target_arn="arn:worker")
    return polls, cursors


def run_fire(name, polls, cursors, payload, status=200):
    """One scheduled fire against a canned provider page."""
    transport = Transport(payload, status)
    fired = []
    with patch.object(provider, "_default_transport", transport), \
         patch.object(poll_triggers, "_bearer_token", return_value="fresh-token"), \
         patch("src.dapier.engine.execute",
               side_effect=lambda event, **_kwargs: fired.append(event)), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(name, table_ref=polls, cursor_table_ref=cursors)
    return result, fired


def test_sheets_fire_seeds_then_emits_only_the_new_row():
    polls, cursors = saved_trigger(sheets_body())

    result, fired = run_fire("invoices-rows", polls, cursors, sheet_values(
        ["2026-09-26", "INV-1", "9.00"]))
    assert result == {"poll": "invoices-rows", "fired": 0}
    assert poll_triggers.get_cursor("invoices-rows", table=cursors) == "2"

    result, fired = run_fire("invoices-rows", polls, cursors, sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"]))

    assert result == {"poll": "invoices-rows", "fired": 1}
    assert len(fired) == 1
    event = fired[0]
    assert event["connector"] == "google-sheets"
    assert event["event"] == "row.new"
    assert event["data"]["poll"] == "invoices-rows"
    assert event["data"]["item_id"] == "3"
    assert event["data"]["Invoice"] == "INV-2"
    assert poll_triggers.get_cursor("invoices-rows", table=cursors) == "3"


def test_updates_fire_seeds_then_emits_only_the_edited_row():
    polls, cursors = saved_trigger(updates_body())

    result, fired = run_fire("invoices-edits", polls, cursors, sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"]))
    assert result == {"poll": "invoices-edits", "fired": 0}
    seed = poll_triggers.get_cursor("invoices-edits", table=cursors)
    assert sorted(json.loads(seed)["rows"]) == ["2", "3"]  # the snapshot, not a row number

    result, fired = run_fire("invoices-edits", polls, cursors, sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "42.00"]))

    assert result == {"poll": "invoices-edits", "fired": 1}
    event = fired[0]
    assert event["connector"] == "google-sheets"
    assert event["event"] == "row.updated"
    assert event["data"]["poll"] == "invoices-edits"
    assert event["data"]["row"] == 3
    assert event["data"]["Invoice"] == "INV-2"
    assert event["data"]["Amount"] == "42.00"
    assert event["data"]["item_id"].startswith("3:")

    # the same sheet again: the edit is not news twice
    result, fired = run_fire("invoices-edits", polls, cursors, sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "42.00"]))
    assert result == {"poll": "invoices-edits", "fired": 0}


def test_rows_and_updates_cursors_stay_independent():
    polls, cursors, events = FakePollTable(), FakeCursorTable(), FakeEvents()
    for body in (sheets_body(), updates_body()):
        poll_triggers.api_save(body, "op@example.test", table_ref=polls,
                               cursor_table_ref=cursors, events_client=events,
                               target_arn="arn:worker")
    page = sheet_values(["2026-09-26", "INV-1", "9.00"],
                        ["2026-09-27", "INV-2", "18.00"])
    run_fire("invoices-rows", polls, cursors, page)
    run_fire("invoices-edits", polls, cursors, page)  # both seed quietly

    # one edit: the updates trigger fires, the rows trigger stays quiet —
    # each walked its own cursor under its own poll name
    edited = sheet_values(["2026-09-26", "INV-1", "9.00"],
                          ["2026-09-27", "INV-2", "77.00"])
    rows_result, rows_fired = run_fire("invoices-rows", polls, cursors, edited)
    updates_result, updates_fired = run_fire("invoices-edits", polls, cursors, edited)

    assert rows_result["fired"] == 0 and rows_fired == []
    assert poll_triggers.get_cursor("invoices-rows", table=cursors) == "3"  # still the row-number watermark
    assert updates_result["fired"] == 1
    assert updates_fired[0]["event"] == "row.updated"
    assert sorted(json.loads(
        poll_triggers.get_cursor("invoices-edits", table=cursors))["rows"]) == ["2", "3"]


def test_drive_fire_seeds_then_emits_only_the_new_file():
    polls, cursors = saved_trigger(drive_body())

    result, fired = run_fire("folder-files", polls, cursors, drive_page(
        "2026-09-26T10:00:00.000Z"))
    assert result == {"poll": "folder-files", "fired": 0}
    seeded = poll_triggers.get_cursor("folder-files", table=cursors)
    assert seeded.endswith("Z")

    result, fired = run_fire("folder-files", polls, cursors, drive_page(
        "2026-09-26T10:00:00.000Z", "2099-01-01T00:00:00.000Z"))

    assert result == {"poll": "folder-files", "fired": 1}
    event = fired[0]
    assert event["connector"] == "google-drive"
    assert event["event"] == "file.created"
    assert event["data"]["poll"] == "folder-files"
    assert event["data"]["item_id"] == "file-1"
    assert event["data"]["name"] == "file-1.pdf"
    assert poll_triggers.get_cursor("folder-files", table=cursors) == "2099-01-01T00:00:00.000Z"


def test_drive_fire_workflow_matches_the_chip():
    polls, cursors = saved_trigger(drive_body())

    workflow = poll_triggers.workflow_for(poll_triggers.get_item(
        "folder-files", table_ref=polls))

    assert workflow["trigger"] == {
        "connector": "google-drive", "event": "file.created",
        "filters": {"poll": {"equals": "folder-files"}}}


# --- the chips' sample pulls ---------------------------------------------------------


@pytest.fixture
def no_history(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


def discover(**body):
    status, payload = trigger_discovery.api_discover(
        {"kind": "sample", **body})
    return status, payload


def test_sheets_sample_pulls_the_polls_newest_row_live(monkeypatch, google_transport,
                                                       no_history):
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"])))
    item = stored(sheets_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="google-sheets", event="invoices-rows")

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["event"] == "row.new"
    assert payload["sample"]["data"]["Invoice"] == "INV-2"  # the newest row
    assert payload["sample"]["data"]["poll"] == "invoices-rows"
    assert payload["connection_id"] == "google"


def test_updates_sample_pulls_the_polls_newest_edit_live(monkeypatch, google_transport,
                                                         no_history):
    item = stored(updates_body())
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "18.00"])))
    _items, snapshot = sheets._sheets_row_updated_fetch(item, None)
    monkeypatch.setattr(poll_triggers, "get_cursor", lambda name, table=None: snapshot)
    google_transport(Transport(sheet_values(
        ["2026-09-26", "INV-1", "9.00"],
        ["2026-09-27", "INV-2", "99.00"])))
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="google-sheets", event="invoices-edits")

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["event"] == "row.updated"
    assert payload["sample"]["data"]["Amount"] == "99.00"  # the edited cell
    assert payload["connection_id"] == "google"


def test_updates_sample_without_a_poll_is_synthetic(monkeypatch, no_history):
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)

    status, payload = discover(connector="google-sheets", event="row.updated")

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "row.updated"
    assert payload["sample"]["data"]["Amount"] == "220.00"  # the edited invoice


def test_drive_sample_pulls_the_folders_newest_file_live(monkeypatch, google_transport,
                                                         no_history):
    google_transport(Transport(drive_page(
        "2026-09-26T10:00:00.000Z", "2026-09-28T10:00:00.000Z")))
    item = stored(drive_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="google-drive", event="folder-files")

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["event"] == "file.created"
    assert payload["sample"]["data"]["id"] == "file-1"  # the newest file
    assert payload["connection_id"] == "google"


def test_sample_without_a_matching_poll_is_synthetic(monkeypatch, no_history):
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)

    status, payload = discover(connector="google-sheets", event="no-such-poll")

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "row.new"
    assert payload["sample"]["data"]["Invoice"]  # a 3-column invoice row
    assert payload["sample"]["data"]["row"]

    status, payload = discover(connector="google-drive")  # no event: same fallback

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "file.created"
    assert payload["sample"]["data"]["mimeType"] == "application/pdf"


def test_sample_falls_back_when_poll_triggers_are_unconfigured(monkeypatch, no_history):
    def unconfigured(name, table_ref=None):
        raise TriggerError("poll triggers are not configured")

    monkeypatch.setattr(poll_triggers, "get_item", unconfigured)

    status, payload = discover(connector="google-sheets", event="invoices-rows")

    assert status == 200, payload
    assert payload["source"] == "synthetic"


def test_sample_ignores_a_poll_with_another_source(monkeypatch, no_history):
    item = stored(sheets_body(source="http", url="https://example.test/list",
                              id_path="id"))
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="google-sheets", event="invoices-rows")

    assert status == 200
    assert payload["source"] == "synthetic"


def test_sample_falls_back_when_the_live_fetch_fails(monkeypatch, google_transport,
                                                     no_history):
    google_transport(Transport({"error": "boom"}, status=500))
    item = stored(sheets_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="google-sheets", event="invoices-rows")

    assert status == 200, payload  # a broken sheet folds to the fallback, never raises
    assert payload["source"] == "synthetic"


def test_sample_uses_the_newest_recorded_run_before_synthetic(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample", lambda connector, event=None: {
        "connector": connector, "event": "row.new",
        "data": {"row": 2, "id": "2", "Invoice": "from history"},
        "id": "run-1", "source": "invoices-rows",
        "occurred_at": "2026-09-26T03:00:00Z"})

    status, payload = discover(connector="google-sheets")

    assert status == 200
    assert payload["source"] == "history"
    assert payload["sample"]["data"]["Invoice"] == "from history"


def test_both_chips_are_in_the_sample_catalog():
    catalog = trigger_discovery.trigger_discovery_catalog()

    assert "google-sheets" in catalog["sample"]
    assert "google-drive" in catalog["sample"]
