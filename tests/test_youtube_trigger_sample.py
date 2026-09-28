"""The YouTube trigger sample's live fetch and its fallbacks.

The chip's sample prefers live data — the connected channel's newest upload
via the playlist_items listing (connectors.youtube._live_upload) — and falls
back to recorded history, then a documented PubSubHubbub notification, so a
sample pull always answers with something an author can build on. The
playlists options listing powers the find-playlist-items action's playlist
field (POST /api/{admin,agent}/discover, kind=options).
"""
import json

import pytest

from src.dapier.connectors import trigger_discovery
import src.dapier.connectors  # noqa: F401  (import = registration)
from src.dapier.connectors import youtube


@pytest.fixture(autouse=True)
def no_recorded_history(monkeypatch):
    """Pin the fallback chain: history depends on run tables."""
    monkeypatch.setattr(
        trigger_discovery, "history_sample",
        lambda connector, event=None: None)


CONNECTION = {"connection_id": "yt-main", "provider": "youtube",
              "status": "connected", "credential_id": "oauth#yt-main"}


class FakeProviderDiscover:
    """Stands in for connections.discovery.discover, routed by resource."""

    def __init__(self, results=None, error=None):
        self.results = results or {}
        self.error = error
        self.calls = []

    def __call__(self, connection, resource, params, *, transport=None):
        self.calls.append((resource, dict(params or {})))
        if self.error is not None:
            raise self.error
        return self.results[resource]


def _connected(monkeypatch, connection=CONNECTION):
    monkeypatch.setattr(
        trigger_discovery, "connected_connection",
        lambda provider, connection_id=None: connection)


def _sample(**kwargs):
    status, payload = trigger_discovery.api_discover(
        {"connector": "youtube", **kwargs})
    assert status == 200, payload
    return payload


def test_live_sample_pulls_the_newest_upload(monkeypatch):
    fake = FakeProviderDiscover(results={
        "channel": [{"id": "UCabc123", "name": "DataTalksClub"}],
        "playlist_items": [{"id": "vid9", "name": "Newest lecture",
                            "published": "2026-09-27T10:00:00Z"}],
    })
    monkeypatch.setattr(youtube.provider, "discover", fake)
    _connected(monkeypatch)

    payload = _sample()
    assert payload["source"] == "live"
    assert payload["connection_id"] == "yt-main"
    assert payload["sample"]["event"] == "video.published"
    assert payload["sample"]["data"] == {
        "video_id": "vid9",
        "channel_id": "UCabc123",
        "title": "Newest lecture",
        "url": "https://www.youtube.com/watch?v=vid9",
    }
    # the uploads playlist is the channel id with UC swapped for UU
    assert fake.calls == [("channel", {}),
                          ("playlist_items", {"playlist_id": "UUabc123"})]


def test_live_sample_honors_the_event_override(monkeypatch):
    fake = FakeProviderDiscover(results={
        "channel": [{"id": "UCabc123", "name": "DataTalksClub"}],
        "playlist_items": [{"id": "vid9", "name": "Newest lecture"}],
    })
    monkeypatch.setattr(youtube.provider, "discover", fake)
    _connected(monkeypatch)
    assert _sample(event="video.published")["sample"]["event"] == "video.published"


def test_no_connected_account_falls_back_to_the_documented_sample(monkeypatch):
    def no_connection(provider, connection_id=None):
        raise trigger_discovery.DiscoveryNotFound(
            f"no connected {provider} connection to discover from")

    monkeypatch.setattr(trigger_discovery, "connected_connection", no_connection)
    payload = _sample()
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "video.published"
    assert payload["sample"]["data"]["video_id"]
    assert payload["sample"]["data"]["url"].startswith(
        "https://www.youtube.com/watch?v=")


def test_live_fetch_failure_falls_through_never_502s(monkeypatch):
    fake = FakeProviderDiscover(error=RuntimeError("connection refused"))
    monkeypatch.setattr(youtube.provider, "discover", fake)
    _connected(monkeypatch)
    payload = _sample()
    assert payload["source"] == "synthetic"
    assert payload["sample"]["data"]["video_id"]


def test_channel_with_no_uploads_falls_back(monkeypatch):
    fake = FakeProviderDiscover(results={
        "channel": [{"id": "UCabc123", "name": "DataTalksClub"}],
        "playlist_items": [],
    })
    monkeypatch.setattr(youtube.provider, "discover", fake)
    _connected(monkeypatch)
    assert _sample()["source"] == "synthetic"


def test_history_wins_when_the_channel_has_no_uploads(monkeypatch):
    fake = FakeProviderDiscover(results={
        "channel": [{"id": "UCabc123", "name": "DataTalksClub"}],
        "playlist_items": [],
    })
    monkeypatch.setattr(youtube.provider, "discover", fake)
    _connected(monkeypatch)
    monkeypatch.setattr(
        trigger_discovery, "history_sample",
        lambda connector, event=None: {
            "connector": "youtube", "event": "video.published",
            "data": {"video_id": "run-vid", "url": "https://www.youtube.com/watch?v=run-vid"},
            "id": "discover-run1", "source": "yt-main",
            "occurred_at": "2026-09-26T03:00:00Z"})
    payload = _sample()
    assert payload["source"] == "history"
    assert payload["sample"]["data"]["video_id"] == "run-vid"


def test_named_connection_must_be_connected(monkeypatch):
    """An explicit id that is not connected is a 502, not a silent fallback."""
    monkeypatch.setattr(
        trigger_discovery, "connected_connection",
        lambda provider, connection_id=None: (_ for _ in ()).throw(
            trigger_discovery.DiscoveryUpstream(
                f"connection '{connection_id}' is not connected; connect it first")))
    status, payload = trigger_discovery.api_discover(
        {"connector": "youtube", "connection_id": "yt-off"})
    assert status == 502
    assert "not connected" in payload["error"]


# --- options: the playlists listing behind the playlist picker --------------------


class FakeHTTP:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


class Table:
    def __init__(self, items=None):
        self.items = items or {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"].get("connection_id"))
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def _configure_connections(monkeypatch, connections):
    import boto3

    tables = {"connections": Table(connections)}

    class Dynamo:
        def Table(self, name):
            return tables[name]

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def test_playlists_options_list_the_connected_channel(monkeypatch):
    from src.dapier.connections import discovery as provider

    transport = FakeHTTP(
        ("playlists?", 200,
         {"items": [{"id": "PL1", "snippet": {"title": "Course 2026"}},
                    {"id": "PL2", "snippet": {"title": "Talks"}}]}))
    monkeypatch.setattr(provider, "_default_transport", transport)
    monkeypatch.setattr(provider.tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))
    _configure_connections(monkeypatch, {
        "yt": {"connection_id": "yt", "provider": "youtube",
               "status": "connected", "credential_id": "oauth#yt"}})

    status, payload = trigger_discovery.api_discover(
        {"connector": "youtube", "kind": "options",
         "resource": "youtube.playlists", "connection_id": "yt"})
    assert status == 200, payload
    assert payload["connector"] == "youtube"
    assert payload["resource"] == "youtube.playlists"
    assert payload["options"] == [{"value": "PL1", "label": "Course 2026"},
                                  {"value": "PL2", "label": "Talks"}]
    assert payload["connection_id"] == "yt"


def test_playlists_options_without_a_connection_are_404(monkeypatch):
    from src.dapier.connections import discovery as provider

    monkeypatch.setattr(provider, "_default_transport", FakeHTTP())
    _configure_connections(monkeypatch, {})
    status, payload = trigger_discovery.api_discover(
        {"connector": "youtube", "kind": "options", "resource": "youtube.playlists"})
    assert status == 404, payload
    assert "no connected youtube connection" in payload["error"]
