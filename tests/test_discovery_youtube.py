"""Connection discovery: the YouTube provider — channel, playlists, and the
videos of one playlist — through the shared domain layer, plus the registry
wiring that exposes those resources to the CLI and console.
"""

import json

import pytest

from src.dapier.connections import discovery
from src.dapier.connections import tokens
from src.dapier.connectors import registry, youtube  # noqa: F401  (registration)


class FakeTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


CONNECTION = {"connection_id": "yt-main", "provider": "youtube",
              "credential_id": "oauth#yt-main"}


@pytest.fixture(autouse=True)
def live_token(monkeypatch):
    monkeypatch.setattr(
        tokens, "get_access_token",
        lambda connection, transport=None: ("tok", {"refreshed": False}))


# --- domain: catalog and fetchers ---


def test_youtube_catalog_lists_channel_playlists_playlist_items_and_videos():
    resources = discovery.resources_for("youtube")
    assert [resource.name for resource in resources] == \
        ["channel", "playlists", "playlist_items", "videos"]
    playlist_items = resources[2]
    assert [(param.name, param.required) for param in playlist_items.params] == \
        [("playlist_id", True)]
    # videos is param-free, so the designer picker can open it with no
    # sibling fields to resolve.
    assert resources[3].params == ()


def test_channel_lists_the_connected_channel(live_token):
    transport = FakeTransport(
        ("channels?part=snippet", 200,
         {"items": [{"id": "UCdatatalks",
                     "snippet": {"title": "DataTalksClub"}}]}))
    items = discovery.discover(CONNECTION, "channel", {}, transport=transport)
    assert items == [{"id": "UCdatatalks", "name": "DataTalksClub"}]
    call = transport.calls[0]
    assert "youtube/v3/channels?" in call["url"] and "mine=true" in call["url"]
    assert call["headers"]["authorization"] == "Bearer tok"


def test_playlists_map_the_owned_playlists(live_token):
    transport = FakeTransport(
        ("playlists?", 200,
         {"items": [{"id": "PL1", "snippet": {"title": "Course 2026"}},
                    {"id": "PL2", "snippet": {"title": "Talks"}}]}))
    items = discovery.discover(CONNECTION, "playlists", {}, transport=transport)
    assert items == [{"id": "PL1", "name": "Course 2026"},
                     {"id": "PL2", "name": "Talks"}]
    call = transport.calls[0]
    assert "youtube/v3/playlists?" in call["url"]
    assert "mine=true" in call["url"] and "maxResults=25" in call["url"]


def test_playlist_items_resolve_video_ids(live_token):
    transport = FakeTransport(
        ("playlistItems?", 200,
         {"items": [
             {"snippet": {"title": "Intro lecture",
                          "publishedAt": "2026-03-04T00:00:00Z"},
              "contentDetails": {"videoId": "vid1"}},
             {"snippet": {"title": "No video id here"}},  # skipped, no id
         ]}))
    items = discovery.discover(CONNECTION, "playlist_items",
                               {"playlist_id": "PL1"}, transport=transport)
    assert items == [{"id": "vid1", "name": "Intro lecture",
                      "published": "2026-03-04T00:00:00Z"}]
    call = transport.calls[0]
    assert "youtube/v3/playlistItems?" in call["url"]
    assert "playlistId=PL1" in call["url"]


def test_playlist_items_require_the_playlist_id(live_token):
    transport = FakeTransport()

    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(CONNECTION, "playlist_items", {}, transport=transport)
    assert excinfo.value.status == 400
    assert transport.calls == []


def test_videos_list_the_channel_uploads(live_token):
    transport = FakeTransport(
        ("channels?part=contentDetails", 200,
         {"items": [{"id": "UCdatatalks",
                     "contentDetails": {"relatedPlaylists":
                                        {"uploads": "UUdatatalks"}}}]}),
        ("playlistItems?", 200,
         {"items": [{"snippet": {"title": "Intro lecture",
                                 "publishedAt": "2026-03-04T00:00:00Z"},
                     "contentDetails": {"videoId": "vid1"}}]}))
    items = discovery.discover(CONNECTION, "videos", {}, transport=transport)
    assert items == [{"id": "vid1", "name": "Intro lecture",
                      "published": "2026-03-04T00:00:00Z"}]
    assert "playlistId=UUdatatalks" in transport.calls[1]["url"]


def test_videos_without_an_uploads_playlist_list_nothing(live_token):
    transport = FakeTransport(
        ("channels?part=contentDetails", 200,
         {"items": [{"id": "UCdatatalks",
                     "contentDetails": {"relatedPlaylists": {}}}]}))
    assert discovery.discover(CONNECTION, "videos", {}, transport=transport) == []
    assert len(transport.calls) == 1


def test_youtube_error_maps_to_502_with_the_provider_message(live_token):
    transport = FakeTransport(
        ("playlists?", 403,
         {"error": {"code": 403, "message": "The request is not properly authorized."}}))

    with pytest.raises(discovery.DiscoveryError) as excinfo:
        discovery.discover(CONNECTION, "playlists", {}, transport=transport)
    assert excinfo.value.status == 502
    assert "not properly authorized" in str(excinfo.value)


# --- registry wiring: what the CLI and console actually serve ---


def test_registry_exposes_every_youtube_catalog_resource():
    catalog = {resource.name for resource in discovery.resources_for("youtube")}
    served = {entry.name for entry in registry.discoveries_for_provider("youtube")}
    assert served == catalog
    for entry in registry.discoveries_for_provider("youtube"):
        assert entry.connector == "youtube"


def test_registry_runners_delegate_to_the_matching_resource(monkeypatch):
    seen = []

    def fake_discover(connection, resource, params, **kwargs):
        seen.append((connection["connection_id"], resource, params))
        return [{"id": "x", "name": "x"}]

    monkeypatch.setattr(discovery, "discover", fake_discover)
    for entry in registry.discoveries_for_provider("youtube"):
        entry.run(CONNECTION, {"a": "b"})
    assert sorted(seen) == sorted([("yt-main", "channel", {"a": "b"}),
                                   ("yt-main", "playlists", {"a": "b"}),
                                   ("yt-main", "playlist_items", {"a": "b"}),
                                   ("yt-main", "videos", {"a": "b"})])


def test_youtube_connection_test_reports_the_channel_identity(monkeypatch):
    from src.dapier.connections.providers import oauth_providers

    monkeypatch.setattr(
        oauth_providers, "verify_account",
        lambda provider, token, transport=None: ("UCdatatalks", "DataTalksClub"))
    verdict = registry.connection_test_for("youtube").run(CONNECTION)
    assert verdict["ok"] is True
    assert verdict["identity"] == {"id": "UCdatatalks", "name": "DataTalksClub"}
