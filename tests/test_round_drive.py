"""The Google Drive poll round: the Drive changes-feed sources
(updated/deleted file) and the share/copy file action staples.

The poll sources follow the dropbox/zoom pattern: save validation through
build_item, a page fetcher returning ``(items, next_cursor)``, a seeded
first fire, and the end-to-end fire against a fake transport with real
cursor and seen machinery — no network, no moto. The Drive changes pair
shares one changes.list fetch (``google-drive.updates`` publishes
``file.updated``, ``google-drive.deletions`` publishes ``file.deleted``,
the page token parked as the cursor). The
actions drive the registered runners with canned HTTP responses, asserting
method, URL, request body and the step-output shape.
"""
import json
import unittest.mock as mock
import urllib.parse

import boto3
import pytest

from src.dapier.connections import discovery, tokens
from src.dapier.engine.actions import base
from plugins.google.runners.drive import run_drive_copy_file, run_drive_share_file
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

import src.dapier.connectors  # noqa: F401  (import = registration)
from src.dapier.connectors import registry

MEMBER_EMAIL = "person@example.test"

EVENT = {"connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"email": MEMBER_EMAIL, "name": "invoice-4137.pdf"}}

GOOGLE_CONNECTION = {"connection_id": "gdrive", "provider": "google",
                     "status": "connected", "credential_id": "oauth#gdrive"}


@pytest.fixture(autouse=True)
def connections_env(monkeypatch):
    """The connections table is configured but empty: every test stubs the
    connection/token/credential seams above it."""
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")

    class Table:
        def get_item(self, **kwargs):
            return {}

    class Dynamo:
        def Table(self, name):
            return Table()

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


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


class JsonTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "body": body})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


class ChangesTransport:
    """The Drive changes feed: startPageToken seeds, canned pages pop per
    call; an exhausted page list drains the feed."""

    def __init__(self, start_token="tok-seed", pages=None):
        self.start_token = start_token
        self.pages = list(pages or [])
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers or {},
                           "body": body})
        assert headers.get("authorization") == "Bearer fresh-token"
        if "startPageToken" in url:
            return 200, json.dumps({"startPageToken": self.start_token}).encode()
        page = (self.pages.pop(0) if self.pages
                else {"changes": [], "newStartPageToken": "tok-drained"})
        return 200, json.dumps(page).encode()


def update_change(change_id, file_id, name, parents=None,
                  time="2026-09-28T10:00:00.000Z"):
    return {"id": change_id, "fileId": file_id, "removed": False, "time": time,
            "file": {"id": file_id, "name": name, "mimeType": "application/pdf",
                     "parents": parents or [], "modifiedTime": time}}


def delete_change(change_id, file_id, time="2026-09-28T11:00:00.000Z"):
    return {"id": change_id, "fileId": file_id, "removed": True, "time": time}


def drive_changes_body(**overrides):
    body = {"name": "drive-edits", "expression": "rate(1 hour)",
            "source": "google-drive.updates", "connection_id": "gdrive",
            "actions": [{"type": "email_send", "to": "ops@example.test"}]}
    body.update(overrides)
    return body


def stored(body):
    """The stored item build_item would persist for ``body``."""
    return poll_triggers.build_item(body, "op@example.test")


def run_drive_fire(item, transport, *, cursors=None):
    """One scheduled fire against a canned changes feed, with real cursor
    and seen machinery and the engine stubbed out."""
    fired_events = []
    cursors = cursors or FakeCursorTable()

    with mock.patch.object(poll_triggers, "get_item", return_value=item), \
         mock.patch.object(poll_triggers, "_bearer_token", return_value="fresh-token"), \
         mock.patch.object(discovery, "_default_transport", side_effect=transport), \
         mock.patch("src.dapier.engine.execute",
                    side_effect=lambda event, **_kwargs: fired_events.append(event)), \
         mock.patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)
    return result, fired_events, cursors


# --- registration: sources, actions, chips -----------------------------------------


def test_the_poll_sources_register_under_their_chip_events():
    for name, connector_name, event in (
            ("google-drive.updates", "google-drive", "file.updated"),
            ("google-drive.deletions", "google-drive", "file.deleted")):
        source = poll_sources.SOURCES[name]
        assert (source.connector, source.event) == (connector_name, event)
        assert name in poll_sources.source_names()


def test_the_new_action_types_are_registered():
    for action_type in ("drive_share_file", "drive_copy_file"):
        assert action_type in registry.ACTIONS


def test_the_chips_declare_the_poll_published_events():
    assert registry.CONNECTORS["google-drive"].events == \
        ("file.created", "file.updated", "file.deleted")


# --- drive actions: share + copy -----------------------------------------------------


def run_share(action, transport):
    with mock.patch.object(base, "_connected_connection",
                           return_value=dict(GOOGLE_CONNECTION)), \
         mock.patch.object(tokens, "get_access_token",
                           return_value=("tok", {})):
        return run_drive_share_file(
            {"connection_id": "gdrive", "file_id": "f-1", **action},
            EVENT, transport=transport)


def run_copy(action, transport):
    with mock.patch.object(base, "_connected_connection",
                           return_value=dict(GOOGLE_CONNECTION)), \
         mock.patch.object(tokens, "get_access_token",
                           return_value=("tok", {})):
        return run_drive_copy_file(
            {"connection_id": "gdrive", "file_id": "f-1", **action},
            EVENT, transport=transport)


def test_share_file_creates_a_user_permission():
    transport = JsonTransport(
        ("/permissions", 200, {"id": "perm-1", "role": "writer",
                               "type": "user"}))

    output = run_share({"role": "writer",
                        "email_address": "reader@example.test"}, transport)

    assert output == {"shared": True, "file_id": "f-1", "permission_id": "perm-1",
                      "role": "writer", "type": "user"}
    call = transport.calls[0]
    assert call["method"] == "POST"
    parsed = urllib.parse.urlparse(call["url"])
    assert (parsed.netloc, parsed.path) == \
        ("www.googleapis.com", "/drive/v3/files/f-1/permissions")
    query = urllib.parse.parse_qs(parsed.query)
    assert query["supportsAllDrives"] == ["true"]
    assert query["fields"] == ["id,role,type,emailAddress"]
    assert call["headers"]["authorization"] == "Bearer tok"
    assert call["headers"]["content-type"] == "application/json"
    assert json.loads(call["body"]) == {
        "role": "writer", "type": "user", "emailAddress": "reader@example.test"}


def test_share_file_with_anyone_needs_no_email():
    transport = JsonTransport(
        ("/permissions", 200, {"id": "perm-2", "role": "reader",
                               "type": "anyone"}))

    output = run_share({"share_type": "anyone"}, transport)

    assert output["type"] == "anyone"
    assert json.loads(transport.calls[0]["body"]) == {
        "role": "reader", "type": "anyone"}


def test_share_file_rejects_bad_fields_and_a_missing_grantee():
    transport = JsonTransport(("/permissions", 200, {"id": "perm-3"}))

    with pytest.raises(ValueError, match="role must be one of"):
        run_share({"role": "owner"}, transport)
    with pytest.raises(ValueError, match="share_type must be one of"):
        run_share({"share_type": "link"}, transport)
    with pytest.raises(ValueError, match="needs an email_address"):
        run_share({"share_type": "user"}, transport)
    with pytest.raises(ValueError, match="file_id"):
        run_share({"file_id": ""}, transport)
    assert transport.calls == []  # nothing reached the API


def test_share_file_provider_error_is_readable():
    transport = JsonTransport(
        ("/permissions", 404, {"error": {"message": "File not found"}}))

    with pytest.raises(RuntimeError) as excinfo:
        run_share({"share_type": "anyone"}, transport)
    assert "HTTP 404" in str(excinfo.value)
    assert "File not found" in str(excinfo.value)


def test_copy_file_posts_files_copy_and_returns_the_upload_keys():
    transport = JsonTransport(
        ("/copy", 200, {"id": "f-2", "name": "invoice-4137 (copy).pdf",
                        "mimeType": "application/pdf", "size": "51200",
                        "webViewLink": "https://drive.google.com/file/d/f-2/view"}))

    output = run_copy({"name": "invoice-4137 (copy).pdf"}, transport)

    assert output == {"file_id": "f-2", "name": "invoice-4137 (copy).pdf",
                      "mime_type": "application/pdf", "size": "51200",
                      "webViewLink": "https://drive.google.com/file/d/f-2/view"}
    call = transport.calls[0]
    assert call["method"] == "POST"
    parsed = urllib.parse.urlparse(call["url"])
    assert (parsed.netloc, parsed.path) == \
        ("www.googleapis.com", "/drive/v3/files/f-1/copy")
    assert urllib.parse.parse_qs(parsed.query)["fields"] == \
        ["id,name,mimeType,size,webViewLink"]
    assert json.loads(call["body"]) == {"name": "invoice-4137 (copy).pdf"}


def test_copy_file_without_a_name_sends_empty_metadata():
    transport = JsonTransport(
        ("/copy", 200, {"id": "f-3", "name": "Copy of report.pdf"}))

    output = run_copy({}, transport)

    assert output["file_id"] == "f-3"
    assert output["mime_type"] is None  # the copy response carried no mimeType
    assert json.loads(transport.calls[0]["body"]) == {}


def test_copy_file_dispatches_through_the_registry():
    transport = JsonTransport(("/copy", 200, {"id": "f-9", "name": "c.pdf"}))

    with mock.patch.object(base, "_connected_connection",
                           return_value=dict(GOOGLE_CONNECTION)), \
         mock.patch.object(tokens, "get_access_token",
                           return_value=("tok", {})), \
         mock.patch.object(base, "_default_transport", transport):
        output = registry.ACTIONS["drive_copy_file"].run(
            {"type": "drive_copy_file", "connection_id": "gdrive",
             "file_id": "f-1"}, EVENT, "wf-1")

    assert output["file_id"] == "f-9"


# --- google-drive.updates / google-drive.deletions poll sources ----------------------


def changes_fetch(source, item, cursor, transport):
    """One direct source fetch with the connection's token refresh stubbed —
    the refresh itself is poll_triggers' job (tested there), not duplicated."""
    with mock.patch.object(poll_triggers, "_bearer_token",
                           return_value="fresh-token"):
        return poll_sources.SOURCES[source].fetch(item, cursor, transport=transport)


def test_a_changes_poll_builds_a_next_cursor_poll():
    item = stored(drive_changes_body(folder_id="folder-1"))

    assert item["source"] == "google-drive.updates"
    assert item["connection_id"] == "gdrive"
    assert item["folder_id"] == "folder-1"
    assert item["cursor_mode"] == "next_cursor"
    assert item["id_path"] == "id"
    assert item["url"] == ""


def test_a_changes_poll_requires_the_connection():
    with pytest.raises(TriggerError, match="connection_id"):
        stored(drive_changes_body(connection_id="  "))
    item = stored(drive_changes_body(source="google-drive.deletions"))
    assert item["source"] == "google-drive.deletions"


def test_the_changes_view_shows_the_watched_folder():
    view = poll_triggers.public_view(stored(drive_changes_body()))

    assert view["source"] == "google-drive.updates"
    assert view["folder_id"] is None


def test_the_first_changes_fetch_seeds_at_the_feeds_edge():
    transport = ChangesTransport(start_token="tok-edge")
    item = stored(drive_changes_body())

    items, seed = changes_fetch("google-drive.updates", item, None, transport)

    assert items == []  # the drive's existing state is history
    assert seed == "tok-edge"
    assert transport.calls[0]["url"].startswith(
        "https://www.googleapis.com/drive/v3/changes/startPageToken")


def test_the_updates_source_takes_its_half_of_the_feed():
    transport = ChangesTransport(pages=[{
        "nextPageToken": "tok-next",
        "changes": [update_change("c-1", "f-1", "edited.pdf"),
                    delete_change("c-2", "f-2")],
    }])
    item = stored(drive_changes_body())

    items, next_token = changes_fetch(
        "google-drive.updates", item, "tok-seed", transport)

    assert [entry["file_id"] for entry in items] == ["f-1"]
    assert items[0]["name"] == "edited.pdf"
    assert items[0]["removed"] is False
    assert items[0]["change_time"] == "2026-09-28T10:00:00.000Z"
    assert next_token == "tok-next"
    parsed = urllib.parse.urlparse(transport.calls[0]["url"])
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/drive/v3/changes"
    assert query["pageToken"] == ["tok-seed"]
    assert query["includeRemoved"] == ["true"]


def test_the_deletions_source_takes_the_other_half():
    transport = ChangesTransport(pages=[{
        "newStartPageToken": "tok-edge",
        "changes": [update_change("c-1", "f-1", "kept.pdf"),
                    delete_change("c-2", "f-2")],
    }])
    item = stored(drive_changes_body(source="google-drive.deletions"))

    items, next_token = changes_fetch(
        "google-drive.deletions", item, "tok-seed", transport)

    assert [entry["file_id"] for entry in items] == ["f-2"]
    assert items[0]["removed"] is True
    assert items[0]["name"] is None  # a deletion carries no file metadata
    assert next_token == "tok-edge"  # the feed drained to its new edge


def test_updates_scope_to_the_stored_folder():
    transport = ChangesTransport(pages=[{
        "nextPageToken": "tok-next",
        "changes": [update_change("c-1", "f-1", "in.pdf", parents=["folder-1"]),
                    update_change("c-2", "f-2", "out.pdf", parents=["folder-2"])],
    }])
    item = stored(drive_changes_body(folder_id="folder-1"))

    items, _next = changes_fetch("google-drive.updates", item, "tok-seed", transport)

    assert [entry["file_id"] for entry in items] == ["f-1"]


def test_a_changes_page_without_a_continuation_token_fails_the_fetch():
    transport = JsonTransport(
        ("/drive/v3/changes", 200, {"changes": [update_change("c-1", "f-1", "x.pdf")]}))
    item = stored(drive_changes_body())

    with pytest.raises(RuntimeError, match="continuation token"):
        changes_fetch("google-drive.updates", item, "tok-seed", transport)


def test_a_failed_changes_fetch_raises_runtimeerror():
    class Unauthorized(ChangesTransport):
        def __call__(self, method, url, *, headers=None, body=None, timeout=15):
            if "startPageToken" not in url:
                return 401, b'{"error": {"code": 401}}'
            return super().__call__(method, url, headers=headers, body=body,
                                    timeout=timeout)

    item = stored(drive_changes_body())

    with pytest.raises(RuntimeError, match="drive poll failed"):
        changes_fetch("google-drive.updates", item, "tok-seed", Unauthorized())


def test_a_changes_poll_without_its_connection_never_lists():
    item = stored(drive_changes_body())
    del item["connection_id"]

    with pytest.raises(RuntimeError, match="connection_id"):
        changes_fetch("google-drive.updates", item, None, ChangesTransport())


def test_fire_seeds_then_emits_the_feed_once():
    item = stored(drive_changes_body())
    cursors = FakeCursorTable()

    result, fired, cursors = run_drive_fire(
        item, ChangesTransport(start_token="tok-seed"), cursors=cursors)

    assert result == {"poll": "drive-edits", "fired": 0}
    assert fired == []
    assert poll_triggers.get_cursor("drive-edits", table=cursors) == "tok-seed"

    page = {"nextPageToken": "tok-next",
            "changes": [update_change("c-1", "f-1", "edited.pdf"),
                        delete_change("c-2", "f-2")]}
    result, fired, cursors = run_drive_fire(
        item, ChangesTransport(pages=[page]), cursors=cursors)

    assert result == {"poll": "drive-edits", "fired": 1}
    event = fired[0]
    assert event["connector"] == "google-drive"
    assert event["event"] == "file.updated"
    assert event["source"] == "drive-edits"
    assert event["data"]["item_id"] == "f-1"
    assert event["data"]["name"] == "edited.pdf"
    assert event["data"]["poll"] == "drive-edits"
    assert poll_triggers.get_cursor("drive-edits", table=cursors) == "tok-next"

    # The refetched page repeats the edit: the seen-set keeps it quiet.
    result, fired, _ = run_drive_fire(
        item, ChangesTransport(pages=[dict(page)]), cursors=cursors)

    assert result["fired"] == 0
    assert fired == []


def test_the_deletions_poll_fires_file_deleted():
    item = stored(drive_changes_body(source="google-drive.deletions"))
    cursors = FakeCursorTable()

    result, fired, cursors = run_drive_fire(
        item, ChangesTransport(start_token="tok-seed"), cursors=cursors)

    assert result["fired"] == 0

    result, fired, _ = run_drive_fire(
        item, ChangesTransport(pages=[{
            "newStartPageToken": "tok-edge",
            "changes": [delete_change("c-2", "f-2"),
                        update_change("c-1", "f-1", "kept.pdf")]}]),
        cursors=cursors)

    assert result == {"poll": "drive-edits", "fired": 1}
    event = fired[0]
    assert event["connector"] == "google-drive"
    assert event["event"] == "file.deleted"
    assert event["data"]["file_id"] == "f-2"
    assert poll_triggers.get_cursor("drive-edits", table=cursors) == "tok-edge"


def test_the_fired_drive_event_matches_the_chip():
    event = poll_triggers.event_for(stored(drive_changes_body()), {"id": "f-1"})

    assert event["connector"] == "google-drive"
    assert event["event"] == "file.updated"
    assert event["data"]["poll"] == "drive-edits"
