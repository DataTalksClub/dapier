"""The drive changes.list poll sources: google-drive.updates (file.updated)
and google-drive.deletions (file.deleted).

Zapier's "Updated File" / "Deleted File" without snapshotting: a stored
poll trigger with one of the changes sources walks Drive's change feed on
the poll schedule — an opaque page-token cursor (seeded on the first fire
so enabling fires nothing), one event per change, folder-scoped updates
and Drive-wide deletions (a removal change carries a fileId only). Each
source keeps its own cursor and seen-set, so both walk the feed
independently. Transport is stubbed at the shared provider seam, the same
way tests/test_google_triggers.py stubs Google listings.
"""
import json
from unittest.mock import patch

import pytest

from src.dapier.connectors import trigger_discovery
from plugins.google.connector import drive
from src.dapier.connections import discovery as provider
from src.dapier.engine.actions import base
from src.dapier.connections import tokens
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

FOLDER_ID = "fold-4137"
FILE_ID = "1a2B3c4D5e6F7g8H9i0J"


def changes_page(*changes, next_token="tok-next", new_token=None):
    """A changes.list body; the trailing token pair picks the drained shape."""
    page = {"changes": list(changes)}
    if next_token:
        page["nextPageToken"] = next_token
    if new_token:
        page["newStartPageToken"] = new_token
        page.pop("nextPageToken", None)
    return page


def updated_change(file_id=FILE_ID, time="2026-09-28T10:00:00.000Z", **file):
    return {"fileId": file_id, "removed": False, "time": time,
            "file": {"id": file_id, "name": "invoices-2026-09.pdf",
                     "mimeType": "application/pdf", "parents": [FOLDER_ID],
                     "modifiedTime": time, **file}}


def removed_change(file_id=FILE_ID, time="2026-09-28T11:00:00.000Z"):
    return {"fileId": file_id, "removed": True, "time": time}


class Transport:
    """Canned provider responses: startPageToken on the token endpoint,
    changes pages otherwise, recording every call."""

    def __init__(self, page=None, start_token="tok-100"):
        self.page = page if page is not None else changes_page()
        self.start_token = start_token
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers})
        if "startPageToken" in url:
            return 200, json.dumps({"startPageToken": self.start_token}).encode()
        return 200, json.dumps(self.page).encode()


def updates_body(**overrides):
    body = {
        "name": "folder-edits",
        "expression": "rate(1 hour)",
        "source": "google-drive.updates",
        "folder_id": FOLDER_ID,
        "connection_id": "google",
        "actions": [{"type": "email_send", "to": "ops@example.test"}],
    }
    body.update(overrides)
    return body


def deletions_body(**overrides):
    body = {
        "name": "drive-deletions",
        "expression": "rate(1 hour)",
        "source": "google-drive.deletions",
        "connection_id": "google",
        "actions": [{"type": "email_send", "to": "ops@example.test"}],
    }
    body.update(overrides)
    return body


def stored(body):
    """The stored item build_item would persist for ``body``."""
    return poll_triggers.build_item(body, "op@example.test")


def stub_google(monkeypatch):
    """The connection record and its (refreshed) OAuth token, without boto3."""
    monkeypatch.setattr(base, "_connected_connection",
                        lambda connection_id: {
                            "connection_id": connection_id, "provider": "google",
                            "status": "connected"})
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, *, transport=None: ("fresh-token", {}))


@pytest.fixture(autouse=True)
def fresh_token(monkeypatch):
    """The poll fetch's bearer seam resolves without DynamoDB (the
    google_transport fixture in test_google_triggers does the same)."""
    monkeypatch.setattr(poll_triggers, "_bearer_token",
                        lambda connection_id: "fresh-token")


def run_fire(item, transport, *, cursors=None):
    """One scheduled fire against a canned feed, with real cursor/seen
    machinery and the engine stubbed out."""
    fired_events = []
    cursors = cursors or FakeCursorTable()

    with patch.object(poll_triggers, "get_item", return_value=item), \
         patch.object(provider, "_default_transport", transport), \
         patch.object(poll_triggers, "_bearer_token",
                      lambda connection_id: "fresh-token"), \
         patch("src.dapier.engine.execute",
               side_effect=lambda event, **_kwargs: fired_events.append(event)), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)
    return result, fired_events, cursors


class FakeCursorTable:
    """DynamoDB stand-in for poll cursors and seen-sets."""

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


# --- registration -----------------------------------------------------------------


def test_both_changes_sources_register_under_the_drive_chip():
    updates = poll_sources.SOURCES["google-drive.updates"]
    deletions = poll_sources.SOURCES["google-drive.deletions"]

    assert (updates.connector, updates.event) == ("google-drive", "file.updated")
    assert (deletions.connector, deletions.event) == ("google-drive", "file.deleted")
    names = poll_sources.source_names()
    assert "google-drive.updates" in names and "google-drive.deletions" in names


# --- save validation --------------------------------------------------------------


def test_updates_save_stores_the_fetch_spec():
    item = stored(updates_body())

    assert item["source"] == "google-drive.updates"
    assert item["folder_id"] == FOLDER_ID
    assert item["connection_id"] == "google"
    assert item["cursor_mode"] == "next_cursor"
    assert item["id_path"] == "id"
    assert item["url"] == ""


def test_updates_save_requires_the_connection():
    with pytest.raises(TriggerError, match="connection_id"):
        stored(updates_body(connection_id=""))


def test_deletions_save_needs_no_folder():
    item = stored(deletions_body())

    assert item["folder_id"] == ""  # removals are Drive-wide
    assert item["connection_id"] == "google"


def test_public_view_shows_the_folder():
    view = poll_triggers.public_view(stored(updates_body()))

    assert view["source"] == "google-drive.updates"
    assert view["folder_id"] == FOLDER_ID


# --- fetch: seed, then the source's half of the feed ---------------------------------


def test_updates_first_fetch_seeds_the_page_token_without_emitting():
    transport = Transport()
    item = stored(updates_body())

    items, next_cursor = drive._drive_updates_fetch(item, None, transport=transport)

    assert items == []
    assert next_cursor == "tok-100"
    assert "startPageToken" in transport.calls[0]["url"]
    assert transport.calls[0]["headers"]["authorization"] == "Bearer fresh-token"


def test_updates_next_fetch_returns_only_edits_in_the_folder():
    other = "fold-other"
    transport = Transport(changes_page(
        updated_change("file-edit", "2026-09-28T10:00:00.000Z"),
        updated_change("file-away", "2026-09-28T10:05:00.000Z", parents=[other]),
        removed_change("file-gone", "2026-09-28T10:10:00.000Z"),
        next_token="tok-next"))
    item = stored(updates_body())

    items, next_cursor = drive._drive_updates_fetch(item, "tok-100", transport=transport)

    assert [change["file_id"] for change in items] == ["file-edit"]
    assert next_cursor == "tok-next"
    call = transport.calls[-1]
    assert "/changes?" in call["url"] and "pageToken=tok-100" in call["url"]
    assert "includeRemoved=true" in call["url"]


def test_updates_without_a_folder_are_drive_wide():
    transport = Transport(changes_page(
        updated_change("file-edit"),
        updated_change("file-away", parents=["fold-other"]),
        next_token="tok-next"))
    item = stored(updates_body(folder_id=""))

    items, _next = drive._drive_updates_fetch(item, "tok-100", transport=transport)

    assert [change["file_id"] for change in items] == ["file-edit", "file-away"]


def test_updates_carry_the_event_data_shape():
    transport = Transport(changes_page(updated_change(), next_token="tok-next"))
    item = stored(updates_body())

    items, _next = drive._drive_updates_fetch(item, "tok-100", transport=transport)

    assert items == [{
        "id": FILE_ID,
        "file_id": FILE_ID,
        "name": "invoices-2026-09.pdf",
        "mime_type": "application/pdf",
        "parents": [FOLDER_ID],
        "change_time": "2026-09-28T10:00:00.000Z",
        "removed": False,
    }]


def test_deletions_fetch_returns_only_removals_and_ignores_the_folder():
    transport = Transport(changes_page(
        removed_change("file-gone", "2026-09-28T11:00:00.000Z"),
        updated_change("file-edit"),
        next_token="tok-next"))
    item = stored(deletions_body(folder_id=FOLDER_ID))

    items, next_cursor = drive._drive_deletions_fetch(item, "tok-100",
                                                      transport=transport)

    assert items == [{
        "id": "file-gone",
        "file_id": "file-gone",
        "name": None,
        "mime_type": None,
        "parents": [],
        "change_time": "2026-09-28T11:00:00.000Z",
        "removed": True,
    }]
    assert next_cursor == "tok-next"


def test_a_drained_feed_parks_the_new_start_token():
    transport = Transport(changes_page(
        removed_change(), next_token=None, new_token="tok-drained"))
    item = stored(deletions_body())

    _items, next_cursor = drive._drive_deletions_fetch(item, "tok-100",
                                                       transport=transport)

    assert next_cursor == "tok-drained"


def test_a_page_without_a_continuation_token_fails_the_fetch():
    transport = Transport({"changes": [removed_change()]})
    item = stored(deletions_body())

    with pytest.raises(RuntimeError, match="continuation token"):
        drive._drive_deletions_fetch(item, "tok-100", transport=transport)


def test_a_stored_item_without_a_connection_never_walks_the_feed():
    with pytest.raises(RuntimeError, match="connection_id"):
        drive._drive_updates_fetch({"poll_id": "x",
                                    "source": "google-drive.updates"}, "tok-100")


def test_a_failed_feed_fails_the_fetch(monkeypatch):
    def boom(method, url, token, payload, transport=None):
        raise provider.DiscoveryError("drive is down")

    monkeypatch.setattr(provider, "_request", boom)
    item = stored(deletions_body())

    with pytest.raises(RuntimeError, match="drive poll failed"):
        drive._drive_deletions_fetch(item, "tok-100")


# --- end-to-end fire ---------------------------------------------------------------


def test_updates_fire_seeds_then_emits_only_new_edits():
    item = stored(updates_body())
    cursors = FakeCursorTable()
    seed = Transport()

    result, fired, cursors = run_fire(item, seed, cursors=cursors)

    assert result == {"poll": "folder-edits", "fired": 0}
    assert fired == []
    assert poll_triggers.get_cursor("folder-edits", table=cursors) == "tok-100"

    editing = Transport(changes_page(
        updated_change("file-edit", "2026-09-28T10:00:00.000Z"),
        next_token="tok-next"))
    result, fired, cursors = run_fire(item, editing, cursors=cursors)

    assert result == {"poll": "folder-edits", "fired": 1}
    event = fired[0]
    assert event["connector"] == "google-drive"
    assert event["event"] == "file.updated"
    assert event["source"] == "folder-edits"
    assert event["data"]["item_id"] == "file-edit"
    assert event["data"]["file_id"] == "file-edit"
    assert event["data"]["name"] == "invoices-2026-09.pdf"
    assert poll_triggers.get_cursor("folder-edits", table=cursors) == "tok-next"


def test_deletions_fire_emits_the_removal_event():
    item = stored(deletions_body())
    cursors = FakeCursorTable()
    run_fire(item, Transport(), cursors=cursors)

    removing = Transport(changes_page(
        removed_change("file-gone", "2026-09-28T11:00:00.000Z"),
        next_token=None, new_token="tok-drained"))
    result, fired, cursors = run_fire(item, removing, cursors=cursors)

    assert result == {"poll": "drive-deletions", "fired": 1}
    event = fired[0]
    assert event["connector"] == "google-drive"
    assert event["event"] == "file.deleted"
    assert event["data"]["file_id"] == "file-gone"
    assert event["data"]["removed"] is True
    assert poll_triggers.get_cursor("drive-deletions", table=cursors) == "tok-drained"


def test_a_refetched_page_does_not_refire_its_edits():
    """The budget can leave a page undrained; the refetch must skip the
    already-fired change through the seen-set, not re-run it."""
    item = stored(updates_body())
    cursors = FakeCursorTable()
    run_fire(item, Transport(), cursors=cursors)

    page = Transport(changes_page(
        updated_change("file-edit"), next_token="tok-next"))
    result, fired, cursors = run_fire(item, page, cursors=cursors)
    assert result == {"poll": "folder-edits", "fired": 1}

    same_page = Transport(changes_page(
        updated_change("file-edit"), next_token="tok-next"))
    result, fired, _ = run_fire(item, same_page, cursors=cursors)

    assert result == {"poll": "folder-edits", "fired": 0, "skipped_seen": 1}
    assert fired == []


def test_the_fired_events_match_the_chip():
    for body, event_name in ((updates_body(), "file.updated"),
                             (deletions_body(), "file.deleted")):
        event = poll_triggers.event_for(stored(body), {"id": "f-1"})
        assert event["connector"] == "google-drive"
        assert event["event"] == event_name
        # a designer workflow binds to the poll through this field
        assert event["data"]["poll"] == body["name"]


# --- the chip's sample pull --------------------------------------------------------


def pin_no_history(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


def discover(**body):
    status, payload = trigger_discovery.api_discover({"kind": "sample", **body})
    return status, payload


def test_sample_pulls_a_stored_updates_poll_live(monkeypatch):
    pin_no_history(monkeypatch)
    item = stored(updates_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)
    monkeypatch.setattr(poll_triggers, "get_cursor",
                        lambda name, table=None: "tok-100")
    monkeypatch.setattr(provider, "_default_transport", Transport(changes_page(
        updated_change("file-edit", "2026-09-28T10:00:00.000Z"),
        next_token="tok-next")))

    status, payload = discover(connector="google-drive", event="folder-edits")

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["event"] == "file.updated"
    assert payload["sample"]["data"]["file_id"] == "file-edit"
    assert payload["sample"]["data"]["poll"] == "folder-edits"


def test_sample_for_an_unfired_deletions_poll_falls_to_synthetic(monkeypatch):
    """No parked cursor yet (the trigger has not fired): nothing live to
    show — the documented file.deleted example answers, never a raise."""
    pin_no_history(monkeypatch)
    item = stored(deletions_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)
    monkeypatch.setattr(poll_triggers, "get_cursor",
                        lambda name, table=None: None)
    monkeypatch.setattr(provider, "_default_transport", Transport())

    status, payload = discover(connector="google-drive", event="drive-deletions")

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "file.deleted"
    assert payload["sample"]["data"]["removed"] is True


def test_sample_synthetic_fallback_keys_on_the_asked_event(monkeypatch):
    pin_no_history(monkeypatch)
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)

    status, payload = discover(connector="google-drive", event="file.updated")

    assert status == 200, payload
    assert payload["sample"]["event"] == "file.updated"
    assert payload["sample"]["data"]["name"]

    status, payload = discover(connector="google-drive")

    assert payload["sample"]["event"] == "file.created"  # the classic chip ask


def test_sample_without_a_matching_poll_stays_synthetic(monkeypatch):
    pin_no_history(monkeypatch)
    item = stored(updates_body(source="http", url="https://example.test/list",
                               id_path="id"))
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="google-drive", event="folder-edits")

    assert status == 200
    assert payload["source"] == "synthetic"


if __name__ == "__main__":
    pytest.main([__file__])
