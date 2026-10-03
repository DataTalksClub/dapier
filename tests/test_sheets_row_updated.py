"""The google-sheets.updates poll source: Zapier's "New or Updated
Spreadsheet Row" as a sibling of google-sheets.rows.

The values API exposes no per-row modified time, so the source diffs
consecutive listings: the trigger's cursor carries a JSON snapshot of row
number → content digest, and a row listed before AND now with a changed
digest publishes ``google-sheets``/``row.updated`` — same item shape as a
row.new event, with ``id`` = ``<row>:<digest>`` so the fire's seen-set
recognizes a re-edit as fresh news. Covered here: the seeding rule (the
first fire parks the worksheet's digests and emits nothing), the diff
semantics (edited rows fire once with their latest cells, brand-new rows
stay row.new's news, deleted rows never fire, a quiet sheet stays quiet),
and end-to-end fires where the snapshot is parked only after the page
drains — a diff bigger than ``max_items`` refetches and the seen-set
recognizes what already ran. The snapshot stores digests only, so events
carry no previous values.

Isolation: the suite swaps a fresh registry dict into ``poll_sources`` —
mutating or relying on the shared ``SOURCES`` dict breaks the other
poll-source suites, which assert its exact contents.
"""
import json
from unittest.mock import patch

import pytest

from plugins.google.connector import sheets
from src.dapier.connections import discovery
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError


# --- fakes -----------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    """A fresh copy of the poll-source registry for every test in this
    suite: the shared SOURCES dict is never mutated (an in-place swap would
    leak into the other poll-source suites), and no other suite's state can
    leak in here."""
    monkeypatch.setattr(poll_sources, "SOURCES", dict(poll_sources.SOURCES))


def updates_body(**overrides):
    body = {
        "name": "invoices-edits",
        "expression": "rate(5 minutes)",
        "source": "google-sheets.updates",
        "spreadsheet_id": "SS-1",
        "worksheet": "todo",
        "connection_id": "google",
        "actions": [{"type": "email_send", "to": "dest@example.test"}],
    }
    body.update(overrides)
    return body


def build(**overrides):
    return poll_triggers.build_item(updates_body(**overrides), "op@example.test")


def sheet_page(*data_rows):
    """A values.get body: row 1 is the header, the rest are data rows."""
    return json.dumps({"values": [["Task", "Status"], *list(data_rows)]}).encode()


class Transport:
    """One canned values.get page, recording every call."""

    def __init__(self, payload, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers})
        return self.status, self.payload


def source_fetch(item, cursor, transport, status=200):
    """One fetch through the registry entry, token refresh stubbed — the
    refresh itself is poll_triggers' job (tested there), not duplicated."""
    with patch.object(poll_triggers, "_bearer_token", return_value="fresh-token"):
        return poll_sources.SOURCES["google-sheets.updates"].fetch(
            item, cursor, transport=Transport(transport, status))


# --- registration and save-time shape -----------------------------------------------


def test_updates_source_registers_for_the_row_updated_event():
    source = poll_sources.SOURCES["google-sheets.updates"]

    assert (source.connector, source.event) == ("google-sheets", "row.updated")
    assert set(poll_sources.source_names()) >= {"google-sheets.rows",
                                                "google-sheets.updates"}


def test_save_stores_the_same_fetch_spec_as_row_new():
    item = build()

    assert item["source"] == "google-sheets.updates"
    assert item["spreadsheet_id"] == "SS-1"
    assert item["worksheet"] == "todo"
    assert item["connection_id"] == "google"
    assert item["cursor_mode"] == "next_cursor"
    assert item["url"] == ""


@pytest.mark.parametrize("missing", ["spreadsheet_id", "connection_id"])
def test_save_requires_its_params(missing):
    with pytest.raises(TriggerError, match=missing):
        build(**{missing: ""})


# --- the fetch: seed, then a digest diff ---------------------------------------------


def test_first_poll_seeds_silently():
    page = sheet_page(["write blog post", "NEW"], ["call the dentist", "OPEN"])
    item = build()

    items, cursor = source_fetch(item, None, page)

    assert items == []  # enabling a trigger must not fire the sheet's rows
    snapshot = json.loads(cursor)
    assert snapshot["v"] == sheets.SHEETS_SNAPSHOT_VERSION
    assert sorted(snapshot["rows"]) == ["2", "3"]


def test_changed_row_emits_once_with_its_latest_cells():
    item = build()
    _items, seed = source_fetch(item, None, sheet_page(
        ["write blog post", "NEW"], ["call the dentist", "OPEN"]))

    items, cursor = source_fetch(item, seed, sheet_page(
        ["write blog post", "NEW"], ["call the dentist", "DONE"]))

    assert len(items) == 1
    edited = items[0]
    assert edited["row"] == 3
    assert edited["id"].startswith("3:")  # the digest-carrying event id
    assert edited["Task"] == "call the dentist"
    assert edited["Status"] == "DONE"
    assert json.loads(cursor)["rows"]["3"] != json.loads(seed)["rows"]["3"]

    # the same edit is news exactly once: the parked snapshot recognizes it
    again, _cursor = source_fetch(item, cursor, sheet_page(
        ["write blog post", "NEW"], ["call the dentist", "DONE"]))
    assert again == []


def test_two_edits_between_polls_emit_once_with_the_latest_state():
    item = build()
    _items, seed = source_fetch(item, None, sheet_page(
        ["write blog post", "NEW"]))

    # the poll sees state, not history: both edits fold into one event
    items, _cursor = source_fetch(item, seed, sheet_page(
        ["write blog post", "DONE"]))

    assert len(items) == 1
    assert items[0]["Status"] == "DONE"


def test_unchanged_rows_are_silent():
    page = sheet_page(["write blog post", "NEW"], ["call the dentist", "OPEN"])
    item = build()
    _items, seed = source_fetch(item, None, page)

    items, cursor = source_fetch(item, seed, page)

    assert items == []
    assert cursor == seed  # nothing changed, the baseline stands


def test_brand_new_row_records_its_digest_without_emitting():
    item = build()
    _items, seed = source_fetch(item, None, sheet_page(
        ["write blog post", "NEW"]))

    items, cursor = source_fetch(item, seed, sheet_page(
        ["write blog post", "NEW"], ["call the dentist", "OPEN"]))

    assert items == []  # creations are row.new's news, not double-fired here
    assert json.loads(cursor)["rows"] == {
        "2": json.loads(seed)["rows"]["2"],
        "3": json.loads(cursor)["rows"]["3"],  # recorded for later edits
    }


def test_deleted_row_is_silent():
    item = build()
    _items, seed = source_fetch(item, None, sheet_page(
        ["write blog post", "NEW"], ["call the dentist", "OPEN"]))

    items, cursor = source_fetch(item, seed, sheet_page(["write blog post", "NEW"]))

    assert items == []  # deletions never fire
    assert sorted(json.loads(cursor)["rows"]) == ["2"]  # and drop from the baseline


def test_second_poll_after_no_changes_is_silent():
    page = sheet_page(["write blog post", "NEW"])
    item = build()
    _items, seed = source_fetch(item, None, page)

    first, first_cursor = source_fetch(item, seed, page)
    second, second_cursor = source_fetch(item, first_cursor, page)

    assert first == [] and second == []
    assert second_cursor == first_cursor


def test_foreign_cursor_reseeds_without_emitting():
    page = sheet_page(["write blog post", "NEW"])
    item = build()

    items, cursor = source_fetch(item, "3", page)  # a rows-source watermark

    assert items == []  # an unreadable baseline is re-seeded, never diffed
    assert json.loads(cursor)["rows"]


def test_failed_fetch_raises_runtimeerror():
    item = build()
    seed = json.dumps({"v": 1, "spreadsheet_id": "SS-1", "worksheet": "todo",
                       "rows": {"2": "x"}})

    with pytest.raises(RuntimeError, match="HTTP 500"):
        source_fetch(item, seed, json.dumps({"error": "boom"}).encode(), status=500)


# --- end-to-end fires: real cursor/seen machinery, fake tables -----------------------


def run_fire(item, payload, *, cursors=None, max_items=None):
    """One scheduled fire against a canned values.get page."""
    cursors = cursors or FakeCursorTable()
    fired = []

    def fake_execute(event, **_kwargs):
        fired.append(event)

    stored = dict(item)
    if max_items is not None:
        stored["max_items"] = max_items
    with patch.object(poll_triggers, "get_item", return_value=stored), \
         patch.object(poll_triggers, "_bearer_token", return_value="fresh-token"), \
         patch.object(discovery, "_default_transport",
                      side_effect=Transport(sheet_page(*payload))), \
         patch("src.dapier.engine.execute", side_effect=fake_execute), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)
    return result, fired, cursors


def test_fire_seeds_then_emits_the_edited_row_once():
    item = build()
    result, fired, cursors = run_fire(item, [["write blog post", "NEW"]])

    assert result == {"poll": "invoices-edits", "fired": 0}
    seeded = json.loads(poll_triggers.get_cursor(item["poll_id"], table=cursors))
    assert sorted(seeded["rows"]) == ["2"]

    result, fired, cursors = run_fire(
        item, [["write blog post", "DONE"]], cursors=cursors)

    assert result == {"poll": "invoices-edits", "fired": 1}
    event = fired[0]
    assert event["connector"] == "google-sheets"
    assert event["event"] == "row.updated"
    assert event["data"]["poll"] == "invoices-edits"
    assert event["data"]["item_id"].startswith("2:")
    assert event["data"]["Task"] == "write blog post"
    assert event["data"]["Status"] == "DONE"
    parked = json.loads(poll_triggers.get_cursor(item["poll_id"], table=cursors))
    assert parked["rows"]["2"] != seeded["rows"]["2"]

    # the same edit does not fire again on the next schedule tick
    result, fired, _cursors = run_fire(
        item, [["write blog post", "DONE"]], cursors=cursors)
    assert result == {"poll": "invoices-edits", "fired": 0}
    assert fired == []


def test_fire_parks_the_snapshot_only_after_the_page_drains():
    """A diff bigger than max_items refetches against the OLD baseline; the
    seen-set (digest-carrying ids) recognizes what already ran, so the
    leftover rows fire on the next tick — nothing is lost or doubled."""
    item = build()
    _result, _fired, cursors = run_fire(item, [["write blog post", "NEW"],
                                               ["call the dentist", "OPEN"]])
    seeded = json.loads(poll_triggers.get_cursor(item["poll_id"], table=cursors))

    result, fired, cursors = run_fire(
        item, [["write blog post", "DONE"], ["call the dentist", "SENT"]],
        cursors=cursors, max_items=1)

    assert result["fired"] == 1
    assert [event["data"]["row"] for event in fired] == [2]
    # the page did not drain, so the fresh snapshot is NOT parked over the
    # leftover row — the next fire re-diffs against the seeding baseline
    baseline = json.loads(poll_triggers.get_cursor(item["poll_id"], table=cursors))
    assert baseline == seeded

    result, fired, cursors = run_fire(
        item, [["write blog post", "DONE"], ["call the dentist", "SENT"]],
        cursors=cursors, max_items=1)

    assert result["fired"] == 1
    assert [event["data"]["row"] for event in fired] == [3]
    assert result.get("skipped_seen") == 1  # row 2's edit was already run
    parked = json.loads(poll_triggers.get_cursor(item["poll_id"], table=cursors))
    assert sorted(parked["rows"]) == ["2", "3"]

    # both edits are now baseline: a quiet tick stays quiet
    result, fired, _cursors = run_fire(
        item, [["write blog post", "DONE"], ["call the dentist", "SENT"]],
        cursors=cursors)
    assert result == {"poll": "invoices-edits", "fired": 0}
    assert fired == []
