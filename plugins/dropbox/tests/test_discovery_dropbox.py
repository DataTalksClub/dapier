"""Discovery contract tests: Dropbox folders and files over files/list_folder.

Unit tests drive the registered Discovery run callables with a fake
transport; API tests go through ``api.discovery`` — the domain layer both
/api/agent/* and /api/admin/* dispatch to — with the provider HTTP seam
monkeypatched, proving the console and the CLI reach the same behavior.
"""

import json

import pytest

from src.dapier.api import discovery as api_discovery
from src.dapier.api import runs
import plugins.dropbox.plugin as dropbox_connector  # noqa: F401 (registers)
from src.dapier.connectors import registry
from src.dapier.connectors import trigger_discovery
from src.dapier.connections import tokens
from src.dapier.engine.actions import base as action_base

CONNECTION = {"connection_id": "dbx", "provider": "dropbox", "status": "connected",
              "credential_id": "oauth#dbx", "root_path": "/team/shared"}


@pytest.fixture(autouse=True)
def live_token(monkeypatch):
    monkeypatch.setattr(
        tokens, "get_access_token",
        lambda connection, transport=None: ("tok", {}))


def dropbox_transport(pages):
    """A transport serving one page per call, recording each request."""
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return 200, pages[len(calls) - 1]

    transport.calls = calls
    return transport


def discovery(name):
    return registry.DISCOVERIES[f"dropbox.{name}"]


def test_dropbox_exposes_folders_and_files():
    names = [entry.name for entry in registry.discoveries_for_provider("dropbox")]
    assert names == ["files", "folders", "search"]
    for entry in registry.discoveries_for_provider("dropbox"):
        assert entry.label and entry.description
    for entry in registry.discoveries_for_provider("dropbox"):
        if entry.name == "search":
            assert [param["key"] for param in entry.params] == ["query"]
            assert entry.params[0]["required"]
        else:
            assert entry.params[0]["key"] == "path"
            assert not entry.params[0].get("required")


def test_folders_default_to_the_connection_root_path():
    transport = dropbox_transport([
        b'{"entries": ['
        b'  {"id": "id:1", "name": "notes.txt", ".tag": "file",'
        b'   "path_display": "/team/shared/notes.txt"},'
        b'  {"id": "id:2", "name": "Archive", ".tag": "folder",'
        b'   "path_display": "/team/shared/Archive"}'
        b'], "has_more": false}',
    ])
    items = discovery("folders").run(CONNECTION, {}, transport=transport)
    assert items == [{"id": "id:2", "name": "Archive",
                      "path": "/team/shared/Archive"}]
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.dropboxapi.com/2/files/list_folder"
    assert call["headers"]["authorization"] == "Bearer tok"
    assert call["headers"]["content-type"] == "application/json"
    assert json.loads(call["body"]) == {
        "path": "/team/shared", "recursive": False,
        "include_deleted": False, "limit": 300}


def test_explicit_path_param_overrides_the_connection_root():
    transport = dropbox_transport([b'{"entries": [], "has_more": false}'])

    items = discovery("folders").run(CONNECTION, {"path": "/Other"}, transport=transport)

    assert items == []
    assert json.loads(transport.calls[0]["body"])["path"] == "/Other"


def test_files_carry_the_paths_actions_need():
    transport = dropbox_transport([
        b'{"entries": ['
        b'  {"id": "id:1", "name": "invoice.pdf", ".tag": "file",'
        b'   "path_display": "/team/shared/invoice.pdf",'
        b'   "size": 2048, "server_modified": "2026-09-20T10:00:00Z"},'
        b'  {"id": "id:2", "name": "Archive", ".tag": "folder",'
        b'   "path_display": "/team/shared/Archive"}'
        b'], "has_more": false}',
    ])
    items = discovery("files").run(CONNECTION, {}, transport=transport)
    assert items == [{"id": "id:1", "name": "invoice.pdf",
                      "path": "/team/shared/invoice.pdf",
                      "size": 2048, "modified": "2026-09-20T10:00:00Z"}]


def test_listing_follows_the_cursor():
    transport = dropbox_transport([
        b'{"entries": [{"id": "id:1", "name": "One", ".tag": "folder",'
        b' "path_display": "/One"}], "cursor": "cur1", "has_more": true}',
        b'{"entries": [{"id": "id:2", "name": "Two", ".tag": "folder",'
        b' "path_display": "/Two"}], "has_more": false}',
    ])
    items = discovery("folders").run(CONNECTION, {}, transport=transport)
    assert [item["name"] for item in items] == ["One", "Two"]
    assert len(transport.calls) == 2
    assert transport.calls[1]["url"].endswith("/files/list_folder/continue")
    assert json.loads(transport.calls[1]["body"]) == {"cursor": "cur1"}


def test_pagination_is_capped_at_five_pages():
    page = (b'{"entries": [{"id": "id:1", "name": "One", ".tag": "folder",'
            b' "path_display": "/One"}], "cursor": "cur", "has_more": true}')
    transport = dropbox_transport([page] * 10)

    discovery("folders").run(CONNECTION, {}, transport=transport)

    assert len(transport.calls) == 5


# --- API surface: api.discovery, shared by /api/agent/* and /api/admin/* ---


class Table:
    def __init__(self, items):
        self.items = items

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"].get("connection_id"))
        return {"Item": dict(item)} if item else {}


def patch_provider_transport(monkeypatch, pages):
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"url": url, "body": body})
        return 200, pages[len(calls) - 1]

    monkeypatch.setattr(action_base, "_default_transport", transport)
    return calls


def test_api_discover_runs_folders_end_to_end(monkeypatch):
    calls = patch_provider_transport(monkeypatch, [
        b'{"entries": [{"id": "id:2", "name": "Archive", ".tag": "folder",'
        b' "path_display": "/team/shared/Archive"}], "has_more": false}',
    ])
    table = Table({"dbx": CONNECTION})

    status, payload = api_discovery.discover(
        "dbx", "folders", {}, connections_table=table)

    assert status == 200
    assert payload["params"] == {}
    assert payload["items"] == [{"id": "id:2", "name": "Archive",
                                 "path": "/team/shared/Archive"}]
    assert json.loads(calls[0]["body"])["path"] == "/team/shared"


def test_api_discover_accepts_the_full_connector_name(monkeypatch):
    patch_provider_transport(monkeypatch, [b'{"entries": [], "has_more": false}'])
    table = Table({"dbx": CONNECTION})

    status, payload = api_discovery.discover(
        "dbx", "dropbox.files", {}, connections_table=table)

    assert status == 200
    assert payload["resource"] == "files"


def test_api_resources_render_picker_metadata():
    table = Table({"dbx": CONNECTION})

    status, payload = api_discovery.resources("dbx", connections_table=table)

    assert status == 200
    assert payload["provider"] == "dropbox"
    assert [resource["name"] for resource in payload["resources"]] == \
        ["files", "folders", "search"]
    for resource in payload["resources"]:
        if resource["name"] == "search":
            assert resource["params"][0]["required"] is True
        else:
            assert resource["params"][0]["required"] is False


def test_api_discover_unknown_resource_names_the_known_ones():
    table = Table({"dbx": CONNECTION})

    status, payload = api_discovery.discover(
        "dbx", "bogus", {}, connections_table=table)

    assert status == 404
    assert "known: files, folders" in payload["error"]


def test_api_discover_requires_a_connected_connection():
    table = Table({"dbx": dict(CONNECTION, status="pending")})

    status, payload = api_discovery.discover(
        "dbx", "folders", {}, connections_table=table)

    assert status == 400
    assert "not connected" in payload["error"]


def test_delete_action_picks_paths_from_the_files_discovery():
    path_field = next(field for field in registry.ACTIONS["dropbox_delete"].fields
                      if field.get("key") == "path")
    assert path_field["discover"] == {"resource": "dropbox.files"}


# --- trigger samples: one sample per file event, history only when it matches ---
#
# The resolver publishes file.created / file.updated / file.deleted envelopes
# with different data shapes; the sample discovery must answer each event ask
# with its own shape, and recorded history only counts when the replayed
# envelope carries the asked event (a deleted run is never renamed to created).

FILE_DATA_KEYS = {"account_id", "path", "path_lower",
                  "file_id", "rev", "content_hash", "size"}

DELETED_ENVELOPE = {
    "id": "dropbox:dbid:acct1:/invoices/invoice-4137.pdf:deleted",
    "connector": "dropbox",
    "event": "file.deleted",
    "source": "dbid:acct1",
    "occurred_at": "2026-09-27T10:00:00+00:00",
    "data": {"account_id": "dbid:acct1",
             "path": "/Invoices/invoice-4137.pdf",
             "path_lower": "/invoices/invoice-4137.pdf"},
}


@pytest.fixture(autouse=True)
def no_recorded_runs(monkeypatch):
    """The sample chain starts at synthetic: no recorded runs, by default."""
    monkeypatch.setattr(runs, "recent", lambda *args, **kwargs: [])


def _trigger_sample(**body):
    status, payload = trigger_discovery.api_discover({"connector": "dropbox", **body})
    assert status == 200, payload
    return payload


def test_sample_without_event_stays_the_created_example():
    payload = _trigger_sample()
    assert payload["source"] == "synthetic"
    assert payload["event"] == "file.created"
    assert payload["sample"]["event"] == "file.created"
    assert set(payload["sample"]["data"]) == FILE_DATA_KEYS


def test_sample_event_file_updated_keeps_the_resolver_file_shape():
    payload = _trigger_sample(event="file.updated")
    assert payload["source"] == "synthetic"
    assert payload["event"] == "file.updated"
    data = payload["sample"]["data"]
    assert set(data) == FILE_DATA_KEYS
    assert data["rev"] != "discover1"  # an update means the rev moved


def test_sample_event_file_deleted_carries_only_the_gone_path():
    payload = _trigger_sample(event="file.deleted")
    assert payload["source"] == "synthetic"
    assert payload["event"] == "file.deleted"
    data = payload["sample"]["data"]
    assert set(data) == {"account_id", "path", "path_lower"}


def test_sample_unknown_event_falls_back_to_the_created_example():
    payload = _trigger_sample(event="file.renamed")
    assert payload["source"] == "synthetic"
    assert payload["event"] == "file.created"
    assert set(payload["sample"]["data"]) == FILE_DATA_KEYS


def test_history_serves_only_the_matching_event(monkeypatch):
    """A recorded file.deleted run fills a file.deleted ask — and only that."""
    monkeypatch.setattr(runs, "recent", lambda *args, **kwargs: [
        {"run_id": "invoices:evt-9", "connector": "dropbox"}])
    monkeypatch.setattr(runs, "api_get", lambda run_id: (200, {"steps": []}))
    monkeypatch.setattr(runs, "replay_event",
                        lambda run_id, steps: (DELETED_ENVELOPE, None))

    deleted = _trigger_sample(event="file.deleted")
    assert deleted["source"] == "history"
    assert deleted["sample"]["event"] == "file.deleted"
    assert deleted["sample"]["data"] == DELETED_ENVELOPE["data"]
    assert deleted["sample"]["id"] == DELETED_ENVELOPE["id"]

    created = _trigger_sample(event="file.created")
    assert created["source"] == "synthetic"
    assert created["event"] == "file.created"  # history is never renamed


def test_history_does_not_fill_the_default_ask_with_other_events(monkeypatch):
    """No event means the file.created ask: a deleted run stays out of it."""
    monkeypatch.setattr(runs, "recent", lambda *args, **kwargs: [
        {"run_id": "invoices:evt-9", "connector": "dropbox"}])
    monkeypatch.setattr(runs, "api_get", lambda run_id: (200, {"steps": []}))
    monkeypatch.setattr(runs, "replay_event",
                        lambda run_id, steps: (DELETED_ENVELOPE, None))

    payload = _trigger_sample()
    assert payload["source"] == "synthetic"
    assert payload["event"] == "file.created"
