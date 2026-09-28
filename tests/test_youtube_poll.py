"""The youtube.videos poll source: save validation, the uploads-playlist
fetch semantics (mine-channel default, publishedAt watermark, seeded first
fire, oldest first), the end-to-end fire, and the chip's sample pull with
the stored-poll live branch ahead of the webhook-side chain.

Zapier's "New Video" without PubSubHubbub: a stored poll trigger with
``source: "youtube.videos"`` lists the channel's uploads playlist through
the shared provider layer (``connections.discovery``) on the poll schedule
and publishes the chip's ``youtube``/``video.published`` events in the
notification's data shape. Transport is stubbed at the shared provider
seam, the same way tests/test_trigger_samples.py stubs YouTube listings.
"""
import json
from unittest.mock import patch

import pytest

from src.dapier.connectors import trigger_discovery, youtube as youtube_connector
from src.dapier.connections import discovery as provider
from src.dapier.engine.actions import base
from src.dapier.connections import tokens
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

YOUTUBE_CONNECTION = {"connection_id": "yt", "provider": "youtube",
                      "status": "connected"}
CHANNEL_ID = "UCbW5IB0F8d1MpdW2AhifDzw"


def channel_page():
    """A channels.list (mine=true) body."""
    return {"items": [{"id": CHANNEL_ID, "snippet": {"title": "My Channel"}}]}


def playlist_page(*videos):
    """A playlistItems body, one upload per given (video_id, publishedAt)."""
    return {"items": [
        {"id": f"pl-{video_id}", "snippet": {"title": f"Video {video_id}",
                                             "publishedAt": published,
                                             "resourceId": {"videoId": video_id}},
         "contentDetails": {"videoId": video_id}}
        for video_id, published in videos]}


class Transport:
    """Canned Data API pages, dispatched on the URL like the provider does."""

    def __init__(self, uploads, channel=None, status=200):
        self.uploads = uploads
        self.channel = channel
        self.status = status
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers})
        if "/channels" in url:
            payload = self.channel if self.channel is not None else {"items": []}
        else:
            payload = self.uploads
        return self.status, json.dumps(payload).encode()


def youtube_body(**overrides):
    body = {
        "name": "channel-uploads",
        "expression": "rate(1 hour)",
        "source": "youtube.videos",
        "connection_id": "yt",
        "channel_id": CHANNEL_ID,
        "actions": [{"type": "email_send", "to": "fans@example.test"}],
    }
    body.update(overrides)
    return body


def stored(body):
    """The stored item build_item would persist for ``body``."""
    return poll_triggers.build_item(body, "op@example.test")


def stub_youtube(monkeypatch):
    """The connection record and its (refreshed) OAuth token, without boto3."""
    monkeypatch.setattr(base, "_connected_connection",
                        lambda connection_id: dict(YOUTUBE_CONNECTION))
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, *, transport=None: ("fresh-token", {}))


def run_fire(item, transport, *, cursors=None):
    """One scheduled fire against a canned listing, with real cursor/seen
    machinery and the engine stubbed out."""
    fired_events = []
    cursors = cursors or FakeCursorTable()

    with patch.object(poll_triggers, "get_item", return_value=item), \
         patch.object(base, "_connected_connection",
                      return_value=dict(YOUTUBE_CONNECTION)), \
         patch.object(tokens, "get_access_token",
                      return_value=("fresh-token", {})), \
         patch.object(provider, "_default_transport", side_effect=transport), \
         patch("src.dapier.engine.execute",
               side_effect=lambda event, **_kwargs: fired_events.append(event)), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)
    return result, fired_events, cursors


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


# --- registration -----------------------------------------------------------------


def test_the_youtube_chip_registers_a_poll_source():
    source = poll_sources.SOURCES["youtube.videos"]

    assert (source.connector, source.event) == ("youtube", "video.published")
    assert "youtube.videos" in poll_sources.source_names()


# --- save validation --------------------------------------------------------------


def test_save_stores_the_fetch_spec():
    item = stored(youtube_body())

    assert item["source"] == "youtube.videos"
    assert item["connection_id"] == "yt"
    assert item["channel_id"] == CHANNEL_ID
    assert item["cursor_mode"] == "next_cursor"
    assert item["id_path"] == "id"
    assert item["url"] == ""  # non-http sources never carry an endpoint


def test_save_defaults_channel_id_to_the_connections_own_channel():
    item = stored(youtube_body(channel_id=None))

    assert item["channel_id"] == ""  # resolved through channels.mine at fetch


def test_save_requires_the_connection():
    with pytest.raises(TriggerError, match="connection_id"):
        stored(youtube_body(connection_id=""))


def test_public_view_shows_the_channel():
    view = poll_triggers.public_view(stored(youtube_body()))

    assert view["source"] == "youtube.videos"
    assert view["channel_id"] == CHANNEL_ID


# --- fetch: seed, then strictly-newer uploads ---------------------------------------


def test_first_fetch_seeds_at_the_newest_upload_without_emitting(monkeypatch):
    stub_youtube(monkeypatch)
    transport = Transport(playlist_page(
        ("abc111", "2026-09-26T10:00:00Z"),
        ("abc222", "2026-09-28T10:00:00Z")))
    item = stored(youtube_body())

    videos, next_cursor = youtube_connector._youtube_poll_fetch(item, None,
                                                                transport=transport)

    assert videos == []
    assert next_cursor == "2026-09-28T10:00:00Z"  # the newest existing upload
    call = transport.calls[0]
    assert "/playlistItems" in call["url"]
    assert f"playlistId=UU{CHANNEL_ID[2:]}" in call["url"]  # the uploads playlist
    assert call["headers"]["authorization"] == "Bearer fresh-token"


def test_a_stored_channel_skips_the_mine_lookup(monkeypatch):
    stub_youtube(monkeypatch)
    transport = Transport(playlist_page(("abc111", "2026-09-26T10:00:00Z")))
    item = stored(youtube_body())

    youtube_connector._youtube_poll_fetch(item, None, transport=transport)

    assert len(transport.calls) == 1  # no channels.list: the channel is stored


def test_an_empty_channel_id_resolves_through_channels_mine(monkeypatch):
    stub_youtube(monkeypatch)
    transport = Transport(playlist_page(("abc111", "2026-09-26T10:00:00Z")),
                          channel=channel_page())
    item = stored(youtube_body(channel_id=""))

    videos, _next = youtube_connector._youtube_poll_fetch(
        item, "2026-09-01T00:00:00Z", transport=transport)

    assert [video["channel_id"] for video in videos] == [CHANNEL_ID]
    assert len(transport.calls) == 2
    assert "mine=true" in transport.calls[0]["url"]
    assert f"playlistId=UU{CHANNEL_ID[2:]}" in transport.calls[1]["url"]


def test_next_fetch_returns_only_newer_uploads_oldest_first(monkeypatch):
    stub_youtube(monkeypatch)
    transport = Transport(playlist_page(
        ("abc333", "2026-09-28T12:00:00Z"),
        ("abc111", "2026-09-28T10:00:00Z"),
        ("abc222", "2026-09-28T11:00:00Z")))
    item = stored(youtube_body())

    videos, next_cursor = youtube_connector._youtube_poll_fetch(
        item, "2026-09-28T10:00:00Z", transport=transport)

    assert [video["video_id"] for video in videos] == ["abc222", "abc333"]
    assert next_cursor == "2026-09-28T12:00:00Z"


def test_nothing_new_keeps_the_cursor(monkeypatch):
    stub_youtube(monkeypatch)
    transport = Transport(playlist_page(("abc111", "2026-09-26T10:00:00Z")))
    item = stored(youtube_body())

    videos, next_cursor = youtube_connector._youtube_poll_fetch(
        item, "2026-09-28T10:00:00Z", transport=transport)

    assert videos == []
    assert next_cursor == "2026-09-28T10:00:00Z"


def test_items_carry_the_notifications_data_shape(monkeypatch):
    stub_youtube(monkeypatch)
    transport = Transport(playlist_page(("abc111", "2026-09-28T10:00:00Z")))
    item = stored(youtube_body())

    videos, _next = youtube_connector._youtube_poll_fetch(
        item, "2026-09-27T00:00:00Z", transport=transport)

    assert videos == [{
        "id": "abc111",
        "video_id": "abc111",
        "channel_id": CHANNEL_ID,
        "title": "Video abc111",
        "url": "https://www.youtube.com/watch?v=abc111",
        "published": "2026-09-28T10:00:00Z",
    }]


def test_a_stored_item_without_a_connection_never_lists(monkeypatch):
    stub_youtube(monkeypatch)

    with pytest.raises(RuntimeError, match="connection_id"):
        youtube_connector._youtube_poll_fetch({"poll_id": "x",
                                               "source": "youtube.videos"}, None)


def test_an_unresolvable_channel_fails_the_fetch(monkeypatch):
    stub_youtube(monkeypatch)
    transport = Transport(playlist_page(), channel={"items": []})
    item = stored(youtube_body(channel_id=""))

    with pytest.raises(RuntimeError, match="channel"):
        youtube_connector._youtube_poll_fetch(item, None, transport=transport)


def test_a_failed_fetch_raises_runtimeerror(monkeypatch):
    stub_youtube(monkeypatch)
    transport = Transport(playlist_page(), status=500)
    item = stored(youtube_body())

    with pytest.raises(RuntimeError, match="HTTP 500"):
        youtube_connector._youtube_poll_fetch(item, "2026-09-28T10:00:00Z",
                                              transport=transport)


def test_a_missing_connection_fails_the_fetch(monkeypatch):
    monkeypatch.setattr(
        base, "_connected_connection",
        lambda connection_id: (_ for _ in ()).throw(
            ValueError(f"connection {connection_id} is not connected")))
    item = stored(youtube_body())

    with pytest.raises(RuntimeError, match="youtube poll failed"):
        youtube_connector._youtube_poll_fetch(item, None)


# --- end-to-end fire ---------------------------------------------------------------


def test_fire_seeds_then_emits_only_the_new_video():
    item = stored(youtube_body())
    cursors = FakeCursorTable()

    result, fired, cursors = run_fire(item, Transport(playlist_page(
        ("abc111", "2026-09-26T10:00:00Z"))), cursors=cursors)

    assert result == {"poll": "channel-uploads", "fired": 0}
    assert fired == []
    assert poll_triggers.get_cursor("channel-uploads",
                                    table=cursors) == "2026-09-26T10:00:00Z"

    result, fired, _ = run_fire(item, Transport(playlist_page(
        ("abc111", "2026-09-26T10:00:00Z"),
        ("abc222", "2026-09-28T10:00:00Z"))), cursors=cursors)

    assert result == {"poll": "channel-uploads", "fired": 1}
    event = fired[0]
    assert event["connector"] == "youtube"
    assert event["event"] == "video.published"
    assert event["source"] == "channel-uploads"
    assert event["data"]["item_id"] == "abc222"
    assert event["data"]["video_id"] == "abc222"
    assert event["data"]["channel_id"] == CHANNEL_ID
    assert poll_triggers.get_cursor("channel-uploads",
                                    table=cursors) == "2026-09-28T10:00:00Z"


def test_the_fired_workflow_matches_the_chip():
    workflow = poll_triggers.workflow_for(stored(youtube_body()))

    assert workflow["trigger"] == {
        "connector": "youtube", "event": "video.published",
        "filters": {"poll": {"equals": "channel-uploads"}}}


# --- the chip's sample pull --------------------------------------------------------


def pin_no_history(monkeypatch):
    monkeypatch.setattr(trigger_discovery, "history_sample",
                        lambda connector, event=None: None)


def discover(**body):
    status, payload = trigger_discovery.api_discover({"kind": "sample", **body})
    return status, payload


def test_sample_pulls_the_polls_newest_upload_live(monkeypatch):
    pin_no_history(monkeypatch)
    stub_youtube(monkeypatch)
    item = stored(youtube_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)
    monkeypatch.setattr(provider, "_default_transport", Transport(playlist_page(
        ("abc111", "2026-09-26T10:00:00Z"),
        ("abc222", "2026-09-28T10:00:00Z"))))

    status, payload = discover(connector="youtube", event="channel-uploads")

    assert status == 200, payload
    assert payload["source"] == "live"
    assert payload["sample"]["connector"] == "youtube"
    assert payload["sample"]["event"] == "video.published"
    assert payload["sample"]["data"]["video_id"] == "abc222"  # the newest upload
    assert payload["sample"]["data"]["poll"] == "channel-uploads"
    assert payload["connection_id"] == "yt"


def test_sample_without_a_matching_poll_is_the_webhook_chain(monkeypatch):
    """No stored poll (or a dotted per-event ask): the PubSubHubbub-side
    chain answers — recorded history, else the documented notification."""
    pin_no_history(monkeypatch)
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: None)

    status, payload = discover(connector="youtube", event="no-such-poll")

    assert status == 200, payload
    assert payload["source"] == "synthetic"
    assert payload["sample"]["data"]["video_id"]
    assert payload["sample"]["data"]["url"].startswith("https://www.youtube.com/watch?v=")

    status, payload = discover(connector="youtube", event="video.published")

    assert status == 200
    assert payload["source"] == "synthetic"
    assert payload["sample"]["event"] == "video.published"  # a dotted ask never
    # names a poll, so the documented PubSubHubbub entry answers it


def test_sample_ignores_a_poll_with_another_source(monkeypatch):
    pin_no_history(monkeypatch)
    item = stored(youtube_body(source="http", url="https://example.test/list",
                               id_path="id"))
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)

    status, payload = discover(connector="youtube", event="channel-uploads")

    assert status == 200
    assert payload["source"] == "synthetic"


def test_sample_falls_back_when_poll_triggers_are_unconfigured(monkeypatch):
    pin_no_history(monkeypatch)

    def unconfigured(name, table_ref=None):
        raise TriggerError("poll triggers are not configured")

    monkeypatch.setattr(poll_triggers, "get_item", unconfigured)

    status, payload = discover(connector="youtube", event="channel-uploads")

    assert status == 200, payload
    assert payload["source"] == "synthetic"


def test_sample_falls_back_when_the_live_fetch_fails(monkeypatch):
    pin_no_history(monkeypatch)
    stub_youtube(monkeypatch)
    item = stored(youtube_body())
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: item)
    monkeypatch.setattr(provider, "_default_transport",
                        Transport(playlist_page(), status=500))

    status, payload = discover(connector="youtube", event="channel-uploads")

    assert status == 200, payload  # a broken playlist folds to the fallback
    assert payload["source"] == "synthetic"


if __name__ == "__main__":
    pytest.main([__file__])
