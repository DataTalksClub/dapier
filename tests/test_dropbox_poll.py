"""The dropbox.files poll source: save validation, the folder-listing fetch
semantics (seeded first fire, server_modified watermark, oldest first), the
end-to-end fire, the chip's sample pull, and the registry lambdas' transport
passthrough.

Zapier's "New File in Folder" without a Dropbox app: a stored poll trigger
with ``source: "dropbox.files"`` lists one folder through the connector
module's own listing (``_run_entries``, files/list_folder + continue) on the
poll schedule and publishes the chip's ``dropbox``/``file.created`` events.
Transport is stubbed at the engine actions' seam (the listing rides
``_dropbox_rpc``), the same way tests/test_poll_presets.py stubs Google.
"""
import json
from unittest.mock import patch

import pytest

from src.dapier.connectors import dropbox as dropbox_connector
from src.dapier.connectors import trigger_discovery
from src.dapier.engine.actions import base
from src.dapier.connections import tokens
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

DROPBOX_CONNECTION = {"connection_id": "dbx", "provider": "dropbox",
                      "status": "connected", "root_path": ""}


class Transport:
    """One canned files/list_folder page, recording every call."""

    def __init__(self, payload, status=200, pages=None):
        self.payload = payload
        self.status = status
        self.pages = pages or []  # optional per-call payloads (pagination)
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "body": body})
        if len(self.calls) == 1 or not self.pages:
            payload = self.payload
        else:
            payload = self.pages.pop(0)
        return self.status, json.dumps(payload).encode()


class FakeCursorTable:
    """DynamoDB stand-in for poll cursors and seen-sets (cursor_id key)."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["cursor_id"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item):
        self.items[Item["cursor_id"]] = dict(Item)

    def delete_item(self, Key):
        self.items.pop(Key["cursor_id"], None)


def dropbox_entry(file_id, name, modified, tag="file"):
    entry = {".tag": tag, "id": f"id:{file_id}", "name": name,
             "path_display": f"/Invoices/{name}",
             "path_lower": f"/invoices/{name.lower()}"}
    if tag == "file":
        entry["size"] = 51200
        entry["server_modified"] = modified
    return entry


def dropbox_page(*entries, cursor=None):
    page = {"entries": list(entries)}
    if cursor:
        page["has_more"] = True
        page["cursor"] = cursor
    return page


def dropbox_body(**overrides):
    body = {
        "name": "invoice-files",
        "expression": "rate(1 hour)",
        "source": "dropbox.files",
        "path": "/Invoices",
        "connection_id": "dbx",
        "actions": [{"type": "email_send", "to": "billing@example.test"}],
    }
    body.update(overrides)
    return body


def stored(body):
    """The stored item build_item would persist for ``body``."""
    return poll_triggers.build_item(body, "op@example.test")


def stub_dropbox(monkeypatch):
    """The connection record and its (refreshed) OAuth token, without boto3."""
    monkeypatch.setattr(base, "_connected_connection",
                        lambda connection_id: dict(DROPBOX_CONNECTION))
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, *, transport=None: ("fresh-token", {}))


def run_fire(item, transport, *, cursors=None):
    """One scheduled fire against a canned listing, with real cursor/seen
    machinery and the engine stubbed out."""
    fired_events = []
    cursors = cursors or FakeCursorTable()

    with patch.object(poll_triggers, "get_item", return_value=item), \
         patch.object(base, "_connected_connection",
                      return_value=dict(DROPBOX_CONNECTION)), \
         patch.object(tokens, "get_access_token",
                      return_value=("fresh-token", {})), \
         patch.object(base, "_default_transport", side_effect=transport), \
         patch("src.dapier.engine.execute",
               side_effect=lambda event, **_kwargs: fired_events.append(event)), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)
    return result, fired_events, cursors


# --- registration -----------------------------------------------------------------


def test_the_dropbox_chip_registers_a_poll_source():
    source = poll_sources.SOURCES["dropbox.files"]

    assert (source.connector, source.event) == ("dropbox", "file.created")
    assert "dropbox.files" in poll_sources.source_names()


# --- save validation --------------------------------------------------------------


def test_save_stores_the_fetch_spec():
    item = stored(dropbox_body())

    assert item["source"] == "dropbox.files"
    assert item["path"] == "/Invoices"
    assert item["connection_id"] == "dbx"
    assert item["cursor_mode"] == "next_cursor"
    assert item["id_path"] == "id"
    assert item["url"] == ""  # non-http sources never carry an endpoint


@pytest.mark.parametrize("missing", ["path", "connection_id"])
def test_save_requires_its_params(missing):
    with pytest.raises(TriggerError, match=missing):
        stored(dropbox_body(**{missing: ""}))


def test_public_view_shows_the_watched_folder():
    view = poll_triggers.public_view(stored(dropbox_body()))

    assert view["source"] == "dropbox.files"
    assert view["path"] == "/Invoices"


# --- fetch: seed, then strictly-newer files ----------------------------------------


def test_first_fetch_seeds_at_the_newest_file_without_emitting(monkeypatch):
    stub_dropbox(monkeypatch)
    transport = Transport(dropbox_page(
        dropbox_entry("f-1", "invoice-4136.pdf", "2026-09-26T10:00:00Z"),
        dropbox_entry("f-2", "invoice-4137.pdf", "2026-09-28T10:00:00Z")))
    item = stored(dropbox_body())

    files, next_cursor = dropbox_connector._dropbox_poll_fetch(item, None,
                                                               transport=transport)

    assert files == []
    assert next_cursor == "2026-09-28T10:00:00Z"  # the newest existing file
    call = transport.calls[0]
    assert call["url"].startswith(dropbox_connector.DROPBOX_LIST_FOLDER_URL)
    assert json.loads(call["body"])["path"] == "/Invoices"
    assert call["headers"]["authorization"] == "Bearer fresh-token"


def test_next_fetch_returns_only_newer_files_oldest_first(monkeypatch):
    stub_dropbox(monkeypatch)
    transport = Transport(dropbox_page(
        dropbox_entry("f-3", "newest.pdf", "2026-09-28T12:00:00Z"),
        dropbox_entry("f-1", "at-cursor.pdf", "2026-09-28T10:00:00Z"),
        dropbox_entry("f-2", "newer.pdf", "2026-09-28T11:00:00Z")))
    item = stored(dropbox_body())

    files, next_cursor = dropbox_connector._dropbox_poll_fetch(
        item, "2026-09-28T10:00:00Z", transport=transport)

    assert [entry["id"] for entry in files] == ["id:f-2", "id:f-3"]  # oldest first
    assert next_cursor == "2026-09-28T12:00:00Z"


def test_nothing_new_keeps_the_cursor(monkeypatch):
    stub_dropbox(monkeypatch)
    transport = Transport(dropbox_page(
        dropbox_entry("f-1", "old.pdf", "2026-09-26T10:00:00Z")))
    item = stored(dropbox_body())

    files, next_cursor = dropbox_connector._dropbox_poll_fetch(
        item, "2026-09-28T10:00:00Z", transport=transport)

    assert files == []
    assert next_cursor == "2026-09-28T10:00:00Z"


def test_folders_never_fire(monkeypatch):
    stub_dropbox(monkeypatch)
    transport = Transport(dropbox_page(
        dropbox_entry("d-1", "Archive", "2026-09-29T10:00:00Z", tag="folder"),
        dropbox_entry("f-1", "invoice.pdf", "2026-09-28T11:00:00Z")))
    item = stored(dropbox_body())

    files, _next = dropbox_connector._dropbox_poll_fetch(
        item, "2026-09-28T10:00:00Z", transport=transport)

    assert [entry["id"] for entry in files] == ["id:f-1"]


def test_the_listing_follows_its_continue_cursor(monkeypatch):
    stub_dropbox(monkeypatch)
    transport = Transport(
        dropbox_page(dropbox_entry("f-1", "old.pdf", "2026-09-26T10:00:00Z"),
                     cursor="page-2"),
        pages=[dropbox_page(dropbox_entry("f-2", "new.pdf",
                                          "2026-09-28T10:00:00Z"))])
    item = stored(dropbox_body())

    files, _next = dropbox_connector._dropbox_poll_fetch(
        item, "2026-09-25T00:00:00Z", transport=transport)

    assert [entry["id"] for entry in files] == ["id:f-1", "id:f-2"]
    assert len(transport.calls) == 2
    assert transport.calls[1]["url"].startswith(
        dropbox_connector.DROPBOX_LIST_CONTINUE_URL)
    assert json.loads(transport.calls[1]["body"]) == {"cursor": "page-2"}


def test_a_stored_item_without_its_params_never_lists(monkeypatch):
    stub_dropbox(monkeypatch)

    with pytest.raises(RuntimeError, match="connection_id"):
        dropbox_connector._dropbox_poll_fetch({"poll_id": "x", "source": "dropbox.files"},
                                              None)
    item = stored(dropbox_body())
    del item["path"]
    with pytest.raises(RuntimeError, match="path"):
        dropbox_connector._dropbox_poll_fetch(item, None)


def test_a_failed_fetch_raises_runtimeerror(monkeypatch):
    stub_dropbox(monkeypatch)
    transport = Transport(dropbox_page(), status=500)
    item = stored(dropbox_body())

    with pytest.raises(RuntimeError, match="HTTP 500"):
        dropbox_connector._dropbox_poll_fetch(item, "2026-09-28T10:00:00Z",
                                              transport=transport)


def test_a_missing_connection_fails_the_fetch(monkeypatch):
    monkeypatch.setattr(
        base, "_connected_connection",
        lambda connection_id: (_ for _ in ()).throw(
            ValueError(f"connection {connection_id} is not connected")))
    item = stored(dropbox_body())

    with pytest.raises(RuntimeError, match="dropbox poll failed"):
        dropbox_connector._dropbox_poll_fetch(item, None)


# --- end-to-end fire ---------------------------------------------------------------


def test_fire_seeds_then_emits_only_the_new_file():
    item = stored(dropbox_body())
    cursors = FakeCursorTable()

    result, fired, cursors = run_fire(item, Transport(dropbox_page(
        dropbox_entry("f-1", "invoice-4136.pdf", "2026-09-26T10:00:00Z"))),
        cursors=cursors)

    assert result == {"poll": "invoice-files", "fired": 0}
    assert fired == []
    assert poll_triggers.get_cursor("invoice-files",
                                    table=cursors) == "2026-09-26T10:00:00Z"

    result, fired, _ = run_fire(item, Transport(dropbox_page(
        dropbox_entry("f-1", "invoice-4136.pdf", "2026-09-26T10:00:00Z"),
        dropbox_entry("f-2", "invoice-4137.pdf", "2026-09-28T10:00:00Z"))),
        cursors=cursors)

    assert result == {"poll": "invoice-files", "fired": 1}
    event = fired[0]
    assert event["connector"] == "dropbox"
    assert event["event"] == "file.created"
    assert event["source"] == "invoice-files"
    assert event["data"]["item_id"] == "id:f-2"
    assert event["data"]["name"] == "invoice-4137.pdf"
    assert event["data"]["path"] == "/Invoices/invoice-4137.pdf"
    assert poll_triggers.get_cursor("invoice-files",
                                    table=cursors) == "2026-09-28T10:00:00Z"


def test_the_fired_workflow_matches_the_chip():
    item = stored(dropbox_body())

    workflow = poll_triggers.workflow_for(item)

    assert workflow["trigger"] == {
        "connector": "dropbox", "event": "file.created",
        "filters": {"poll": {"equals": "invoice-files"}}}


# --- the chip's sample pull --------------------------------------------------------


def pin_no_history(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


def discover(**body):
    status, payload = trigger_discovery.api_discover({"kind": "sample", **body})
    return status, payload


def test_sample_pulls_the_polls_newest_file_live(monkeypatch):
    pin_no_history(monkeypatch)
    stub_dropbox(monkeypatch)
    item = stored(dropbox_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)
    transport = Transport(dropbox_page(
        dropbox_entry("f-1", "invoice-4136.pdf", "2026-09-26T10:00:00Z"),
        dropbox_entry("f-2", "invoice-4137.pdf", "2026-09-28T10:00:00Z")))
    monkeypatch.setattr(base, "_default_transport", transport)

    status, payload = discover(connector="dropbox", event="invoice-files")

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["connector"] == "dropbox"
    assert payload["sample"]["event"] == "file.created"
    assert payload["sample"]["data"]["name"] == "invoice-4137.pdf"  # the newest
    assert payload["sample"]["data"]["poll"] == "invoice-files"
    assert payload["connection_id"] == "dbx"


def test_sample_without_a_matching_poll_is_synthetic(monkeypatch):
    pin_no_history(monkeypatch)
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)

    status, payload = discover(connector="dropbox", event="no-such-poll")

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "file.created"
    assert payload["sample"]["data"]["path"] == "/Invoices/invoice-4137.pdf"

    status, payload = discover(connector="dropbox", event="file.deleted")

    assert status == 200
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "file.deleted"


def test_sample_ignores_a_poll_with_another_source(monkeypatch):
    pin_no_history(monkeypatch)
    item = stored(dropbox_body(source="http", url="https://example.test/list",
                               id_path="id"))
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="dropbox", event="invoice-files")

    assert status == 200
    assert payload["source"] == "synthetic"


def test_sample_falls_back_when_poll_triggers_are_unconfigured(monkeypatch):
    pin_no_history(monkeypatch)

    def unconfigured(name, table_ref=None):
        raise TriggerError("poll triggers are not configured")

    monkeypatch.setattr(poll_triggers, "get_item", unconfigured)

    status, payload = discover(connector="dropbox", event="invoice-files")

    assert status == 200, payload
    assert payload["source"] == "synthetic"


def test_sample_falls_back_when_the_live_fetch_fails(monkeypatch):
    pin_no_history(monkeypatch)
    stub_dropbox(monkeypatch)
    item = stored(dropbox_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)
    monkeypatch.setattr(base, "_default_transport", Transport(dropbox_page(),
                                                              status=500))

    status, payload = discover(connector="dropbox", event="invoice-files")

    assert status == 200, payload  # a broken folder folds to the fallback
    assert payload["source"] == "synthetic"


def test_sample_uses_the_newest_recorded_run_before_synthetic(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample", lambda connector, event=None: {
        "connector": connector, "event": "file.created",
        "data": {"path": "/Invoices/from-history.pdf"},
        "id": "run-1", "source": "invoice-files",
        "occurred_at": "2026-09-26T03:00:00Z"})

    status, payload = discover(connector="dropbox")

    assert status == 200
    assert payload["source"] == "history"
    assert payload["sample"]["data"]["path"] == "/Invoices/from-history.pdf"


# --- the registry lambdas' transport passthrough ------------------------------------


@pytest.mark.parametrize("action_type,runner", [
    ("dropbox_upload", "run_dropbox_upload"),
    ("dropbox_delete", "run_dropbox_delete"),
    ("dropbox_find", "run_dropbox_find"),
    ("dropbox_read_file", "run_dropbox_read_file"),
    ("dropbox_get_temp_link", "run_dropbox_get_temp_link"),
])
def test_action_lambdas_pass_transport_through(monkeypatch, action_type, runner):
    """Every dropbox registry lambda forwards ``transport`` to its engine
    runner (None when dispatch leaves it unset — the engine's case), like
    the module's discovery and connection-test runners already do."""
    seen = []
    monkeypatch.setattr(dropbox_connector, runner,
                        lambda action, event, transport=None, steps=None:
                        seen.append(transport))
    from src.dapier.connectors import registry

    registry.ACTIONS[action_type].run({}, {}, None, steps=None, transport="t")
    registry.ACTIONS[action_type].run({}, {}, None, steps=None)

    assert seen == ["t", None]
