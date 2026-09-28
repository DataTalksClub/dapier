"""The Zoom recordings poll source: the zoom chip's "New Recording" trigger
without a Zoom app — a stored poll with ``source: "zoom.recordings"`` lists
the connection's cloud recordings on the schedule machinery and publishes
the same ``recording.completed`` events the webhook intake delivers.

Covers the save-time shape (source validate → build_item), the fetch
transform (the 30-day listing through the shared provider helper, the
composite ``start_time|uuid`` watermark, seeding on the first fire, the
MP4/M4V rule the webhook shares), the end-to-end fire, and the chip's
poll-aware sample pull. Zoom calls run through a fake transport, the seen
store and cursors through fake DynamoDB tables — no network, no moto.
"""
import json
import re
import urllib.parse
from unittest.mock import patch

import pytest

from src.dapier.connections import discovery
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

ZOOM_URL = "https://api.zoom.us/v2/users/me/recordings"


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


def build(name="zoom-watch", **body):
    body.setdefault("actions", [{"type": "email_send", "to": "dest@example.test"}])
    body.setdefault("connection_id", "zoom")
    body["source"] = "zoom.recordings"
    body["expression"] = "rate(15 minutes)"
    return poll_triggers.build_item({"name": name, **body}, "op@example.test")


def source_fetch(item, cursor, transport):
    """One direct source fetch with the connection's token refresh stubbed —
    the refresh itself is poll_triggers' job (tested there), not duplicated."""
    with patch.object(poll_triggers, "_bearer_token", return_value="fresh-token"):
        return poll_sources.SOURCES["zoom.recordings"].fetch(
            item, cursor, transport=transport)


def zoom_meeting(meeting_id, uuid_, topic, start, files=None):
    return {
        "uuid": uuid_, "id": meeting_id, "topic": topic, "start_time": start,
        "duration": 45, "host_id": "host-1", "total_size": 184357376,
        "recording_count": 1 if files else 0,
        "recording_files": files or [],
    }


def mp4(file_id):
    return {"id": file_id, "file_type": "MP4", "recording_type": "cloud_recording",
            "file_size": 184357376,
            "play_url": f"https://example.zoom.us/rec/play/{file_id}",
            "download_url": f"https://example.zoom.us/rec/download/{file_id}"}


def transcript(file_id):
    return {"id": file_id, "file_type": "TRANSCRIPT",
            "recording_type": "audio_transcript", "file_size": 18234,
            "play_url": f"https://example.zoom.us/rec/play/{file_id}",
            "download_url": f"https://example.zoom.us/rec/download/{file_id}"}


def zoom_transport(recorder, meetings):
    def transport(method, url, headers=None, body=None, timeout=None):
        recorder.append({"method": method, "url": url, "headers": headers})
        assert method == "GET"
        assert url.startswith(ZOOM_URL)
        assert headers.get("authorization") == "Bearer fresh-token"
        return 200, json.dumps({"meetings": list(meetings)}).encode()
    return transport


def run_fire(item, transport, *, cursors=None):
    """One fire() against ``transport`` with real cursor/seen machinery."""
    fired_events = []
    cursors = cursors or FakeCursorTable()

    def fake_execute(event, **_kwargs):
        fired_events.append(event)

    with patch.object(poll_triggers, "get_item", return_value=item), \
         patch.object(poll_triggers, "_bearer_token", return_value="fresh-token"), \
         patch.object(discovery, "_default_transport", side_effect=transport), \
         patch("src.dapier.engine.execute", side_effect=fake_execute), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)

    return result, fired_events, cursors


# --- save-time shape --------------------------------------------------------------


class TestZoomSave:
    def test_a_zoom_poll_builds_a_seen_store_poll(self):
        item = build(for_email="me")

        assert item["source"] == "zoom.recordings"
        assert item["connection_id"] == "zoom"
        assert item["for_email"] == "me"
        assert item["cursor_mode"] == "next_cursor"  # seen-store dedupe
        assert item["id_path"] == "id"
        assert item["url"] == ""  # the source fetches; no HTTP url

    def test_the_connection_is_required(self):
        with pytest.raises(TriggerError, match="connection_id"):
            build(connection_id="  ")

    def test_for_email_is_optional_and_defaults_to_the_connection_user(self):
        item = build()

        assert item["for_email"] == "me"

    def test_a_named_mailbox_is_stored(self):
        item = build(for_email="host@example.test")

        assert item["for_email"] == "host@example.test"

    def test_the_view_shows_the_polled_mailbox(self):
        view = poll_triggers.public_view(build(for_email="host@example.test"))

        assert view["source"] == "zoom.recordings"
        assert view["for_email"] == "host@example.test"


# --- the fetch ----------------------------------------------------------------------


class TestZoomFetch:
    def item(self, **overrides):
        return build(**overrides)

    def test_the_listing_watches_the_mailboxes_last_30_days(self):
        calls = []
        items, _ = source_fetch(
            self.item(), "2026-09-27T10:00:00Z|old",
            zoom_transport(calls, []))

        assert len(calls) == 1
        parsed = urllib.parse.urlparse(calls[0]["url"])
        query = urllib.parse.parse_qs(parsed.query)
        assert parsed.path == "/v2/users/me/recordings"
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", query["from"][0])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", query["to"][0])
        assert query["per_page"] == ["100"]

    def test_a_named_mailbox_switches_the_listing_path(self):
        """for_email: me (or unset) lists the connection's own user; a named
        host lists that user's recordings, email URL-quoted in the path."""
        calls = []

        def transport(method, url, headers=None, body=None, timeout=None):
            calls.append({"url": url})
            return 200, json.dumps({"meetings": []}).encode()

        source_fetch(self.item(for_email="host@example.test"),
                     "2026-09-27T10:00:00Z|old", transport)
        parsed = urllib.parse.urlparse(calls[0]["url"])
        assert parsed.path == "/v2/users/host%40example.test/recordings"

        calls.clear()
        source_fetch(self.item(), "2026-09-27T10:00:00Z|old", transport)
        assert urllib.parse.urlparse(calls[0]["url"]).path == \
            "/v2/users/me/recordings"

    def test_items_carry_the_webhook_payload_shape(self):
        meeting = zoom_meeting(
            "94839610293", "uuid-1", "Weekly sync", "2026-09-28T10:00:00Z",
            files=[mp4("file-1"), transcript("file-2")])
        items, next_cursor = source_fetch(
            self.item(), "2026-09-27T10:00:00Z|old",
            zoom_transport([], [meeting]))

        entry = items[0]
        assert entry["id"] == "2026-09-28T10:00:00Z|uuid-1"
        assert entry["meeting_id"] == "94839610293"
        assert entry["meeting_uuid"] == "uuid-1"
        assert entry["topic"] == "Weekly sync"
        assert entry["start_time"] == "2026-09-28T10:00:00Z"
        assert entry["download_url"].endswith("/file-1")
        # Metadata only — the video files, and of those the publishable types.
        assert [file["file_type"] for file in entry["video_files"]] == ["MP4"]
        assert next_cursor == "2026-09-28T10:00:00Z|uuid-1"

    def test_recordings_without_a_video_file_never_fire(self):
        """The webhook's own rule: recording.completed publishes only when an
        MP4/M4V exists — a transcript-only recording is not news."""
        transcript_only = zoom_meeting(
            "111", "uuid-t", "Audio only", "2026-09-28T11:00:00Z",
            files=[transcript("file-t")])
        items, next_cursor = source_fetch(
            self.item(), "2026-09-27T10:00:00Z|old",
            zoom_transport([], [transcript_only]))

        assert items == []
        assert next_cursor == "2026-09-27T10:00:00Z|old"

    def test_the_first_fetch_seeds_at_the_newest_recording(self):
        older = zoom_meeting("111", "u-1", "Standup", "2026-09-28T09:00:00Z",
                             files=[mp4("f-1")])
        newer = zoom_meeting("222", "u-2", "Weekly sync", "2026-09-28T10:00:00Z",
                             files=[mp4("f-2")])
        items, seed = source_fetch(
            self.item(), None, zoom_transport([], [newer, older]))

        assert items == []  # the window's existing recordings are history
        assert seed == "2026-09-28T10:00:00Z|u-2"

    def test_only_recordings_past_the_watermark_are_fresh(self):
        older = zoom_meeting("111", "u-1", "Standup", "2026-09-28T09:00:00Z",
                             files=[mp4("f-1")])
        newer = zoom_meeting("222", "u-2", "Weekly sync", "2026-09-28T10:00:00Z",
                             files=[mp4("f-2")])
        items, next_cursor = source_fetch(
            self.item(), "2026-09-28T09:00:00Z|u-1",
            zoom_transport([], [newer, older]))

        assert [entry["meeting_id"] for entry in items] == ["222"]  # oldest first
        assert next_cursor == "2026-09-28T10:00:00Z|u-2"

    def test_a_missing_connection_fails_the_fetch(self):
        item = self.item()
        del item["connection_id"]

        with pytest.raises(RuntimeError, match="connection_id"):
            source_fetch(item, None, zoom_transport([], []))

    def test_a_provider_error_is_a_runtime_error(self):
        def transport(method, url, headers=None, body=None, timeout=None):
            return 401, b'{"message": "invalid token"}'

        with pytest.raises(RuntimeError, match="zoom poll failed"):
            source_fetch(self.item(), "2026-09-27T10:00:00Z|old", transport)


# --- the end-to-end fire ------------------------------------------------------------


class TestZoomFire:
    def item(self):
        return build()

    def test_the_first_fire_seeds_and_fires_nothing(self):
        existing = zoom_meeting("111", "u-1", "Standup", "2026-09-28T09:00:00Z",
                                files=[mp4("f-1")])
        result, fired, cursors = run_fire(
            self.item(), zoom_transport([], [existing]))

        assert result == {"poll": "zoom-watch", "fired": 0}
        assert fired == []
        assert cursors.items["poll#zoom-watch"]["cursor"] == \
            "2026-09-28T09:00:00Z|u-1"

    def test_a_new_recording_fires_as_a_zoom_event(self):
        existing = zoom_meeting("111", "u-1", "Standup", "2026-09-28T09:00:00Z",
                                files=[mp4("f-1")])
        cursors = FakeCursorTable()
        run_fire(self.item(), zoom_transport([], [existing]), cursors=cursors)

        new = zoom_meeting("222", "u-2", "Weekly sync", "2026-09-28T10:00:00Z",
                           files=[mp4("f-2")])
        result, fired, _ = run_fire(
            self.item(), zoom_transport([], [new, existing]), cursors=cursors)

        assert result == {"poll": "zoom-watch", "fired": 1}
        event = fired[0]
        assert event["connector"] == "zoom"
        assert event["event"] == "recording.completed"
        assert event["data"]["poll"] == "zoom-watch"
        assert event["data"]["topic"] == "Weekly sync"
        assert event["data"]["meeting_id"] == "222"

    def test_a_relisted_recording_does_not_fire_twice(self):
        existing = zoom_meeting("111", "u-1", "Standup", "2026-09-28T09:00:00Z",
                                files=[mp4("f-1")])
        cursors = FakeCursorTable()
        run_fire(self.item(), zoom_transport([], [existing]), cursors=cursors)

        new = zoom_meeting("222", "u-2", "Weekly sync", "2026-09-28T10:00:00Z",
                           files=[mp4("f-2")])
        run_fire(self.item(), zoom_transport([], [new, existing]), cursors=cursors)
        second, fired_second, _ = run_fire(
            self.item(), zoom_transport([], [new, existing]), cursors=cursors)

        assert second["fired"] == 0
        assert fired_second == []


# --- the chip's sample pull ---------------------------------------------------------


class TestSamplePull:
    def test_a_stored_zoom_poll_pulls_a_live_recording(self, monkeypatch):
        from src.dapier.connectors import trigger_discovery

        item = build(name="zoom-watch")
        meeting = zoom_meeting("222", "u-2", "Weekly sync",
                               "2026-09-28T10:00:00Z", files=[mp4("f-2")])
        monkeypatch.setattr(poll_triggers, "get_item",
                            lambda name, table_ref=None: item)
        monkeypatch.setattr(poll_triggers, "_bearer_token",
                            lambda connection_id: "fresh-token")
        monkeypatch.setattr(discovery, "_default_transport",
                            zoom_transport([], [meeting]))
        monkeypatch.setattr(trigger_discovery, "history_sample",
                            lambda connector, event=None: None)

        result = trigger_discovery.discover("zoom", event="zoom-watch")

        assert result["source"] == "live"
        assert result["sample"]["event"] == "recording.completed"
        assert result["sample"]["data"]["poll"] == "zoom-watch"
        assert result["sample"]["data"]["topic"] == "Weekly sync"
        assert result["connection_id"] == "zoom"

    def test_a_per_event_ask_never_hits_the_poll_store(self, monkeypatch):
        """Event names carry dots and poll ids cannot — the guard keeps the
        documented per-event payload path table-free."""
        from src.dapier.connectors import trigger_discovery

        monkeypatch.setattr(trigger_discovery, "history_sample",
                            lambda connector, event=None: None)
        looked_up = []
        monkeypatch.setattr(
            poll_triggers, "get_item",
            lambda name, table_ref=None: looked_up.append(name) or None)

        result = trigger_discovery.discover("zoom", event="recording.completed")

        assert looked_up == []
        assert result["source"] == "synthetic"
        assert result["event"] == "recording.completed"
        assert result["sample"]["data"]["video_files"]

    def test_a_failed_live_pull_falls_back_to_the_documented_example(
            self, monkeypatch):
        from src.dapier.connectors import trigger_discovery

        item = build(name="zoom-watch")
        monkeypatch.setattr(poll_triggers, "get_item",
                            lambda name, table_ref=None: item)
        monkeypatch.setattr(poll_triggers, "_bearer_token",
                            lambda connection_id: "fresh-token")
        monkeypatch.setattr(trigger_discovery, "history_sample",
                            lambda connector, event=None: None)

        def transport(method, url, headers=None, body=None, timeout=None):
            return 500, b'{"message": "zoom is down"}'

        monkeypatch.setattr(discovery, "_default_transport", transport)

        result = trigger_discovery.discover("zoom", event="zoom-watch")

        assert result["source"] == "synthetic"
        assert result["sample"]["event"] == "recording.completed"


if __name__ == "__main__":
    pytest.main([__file__])
