"""Find actions: youtube_find_video, zoom_find_meeting, telegram_find_chat.

Unit tests drive each registered runner with a fake provider transport (the
test_discovery FakeTransport pattern) and the connection/token seams
monkeypatched the way the sheets, zoom-discovery, and telegram tests do.
Not-found outcomes are verdicts (``found: False``), never raises.
"""

import json
import unittest.mock as mock
import urllib.parse

import pytest

from src.dapier.connectors import registry
from src.dapier.connections import credentials as credentials_module
from src.dapier.connections import tokens
from src.dapier.connections.providers import telegram_api
from plugins.telegram.runners.telegram import run_telegram_find_chat
from src.dapier.engine.actions.youtube import (
    run_youtube_find_playlist_items,
    run_youtube_find_video,
)
from src.dapier.engine.actions.zoom import (
    run_zoom_create_meeting,
    run_zoom_find_meeting,
    run_zoom_find_recording,
)


class FakeTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes  # (substring, status, payload) tuples
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": body})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


GOOGLE_CONNECTION = {"connection_id": "yt-main", "provider": "youtube",
                     "status": "connected", "credential_id": "oauth#yt-main"}
ZOOM_CONNECTION = {"connection_id": "zoom-main", "provider": "zoom",
                   "status": "connected", "credential_id": "oauth#zoom-main"}
TELEGRAM_CONNECTION = {"connection_id": "tg-bot", "provider": "telegram",
                       "status": "connected", "credential_id": "bot#tg-bot"}

EVENT = {"connector": "telegram", "event": "message.received",
         "occurred_at": "2026-09-27T10:00:00+00:00",
         "data": {"topic": "kubernetes course", "chat_handle": "@dtc_announce",
                  "meeting_id": "9001"}}


@pytest.fixture(autouse=True)
def connections(monkeypatch):
    """Every connection resolves live and every token is a stored secret."""
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))
    monkeypatch.setattr(credentials_module, "get_credential",
                        lambda credential_id: {"token": "123456:AAAtok"})


def with_connection(module_name, connection):
    """Patch the connection seam the way the engine action module resolves it."""
    helper = {"youtube": "_youtube_connection", "zoom": "_zoom_connection"}.get(
        module_name, "_connected_connection")
    module = "base" if module_name == "telegram" else module_name
    return mock.patch(f"src.dapier.engine.actions.{module}.{helper}",
                      return_value=dict(connection))


# --- youtube_find_video -------------------------------------------------------


def run_youtube(action, transport):
    with with_connection("youtube", GOOGLE_CONNECTION):
        return run_youtube_find_video(
            {"type": "youtube_find_video", "connection_id": "yt-main", **action},
            EVENT, transport=transport)


def test_youtube_find_video_returns_top_videos():
    transport = FakeTransport(
        ("youtube/v3/search", 200,
         {"items": [
             {"id": {"videoId": "vid1"},
              "snippet": {"title": "K8s lecture 1", "channelTitle": "DataTalksClub",
                          "publishedAt": "2026-03-04T00:00:00Z"}},
             {"id": {"videoId": "vid2"},
              "snippet": {"title": "K8s lecture 2", "channelTitle": "DataTalksClub",
                          "publishedAt": "2026-03-05T00:00:00Z"}},
             {"id": {"kind": "youtube#searchResult"}},  # no videoId, skipped
         ]}))

    output = run_youtube({"query": "kubernetes"}, transport)

    assert output == {
        "found": True, "count": 2,
        "videos": [
            {"id": "vid1", "title": "K8s lecture 1", "channel": "DataTalksClub",
             "published": "2026-03-04T00:00:00Z"},
            {"id": "vid2", "title": "K8s lecture 2", "channel": "DataTalksClub",
             "published": "2026-03-05T00:00:00Z"},
        ],
        "video": {"id": "vid1", "title": "K8s lecture 1", "channel": "DataTalksClub",
                  "published": "2026-03-04T00:00:00Z"},
    }
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"].startswith("https://www.googleapis.com/youtube/v3/search?")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
    assert query == {"part": ["snippet"], "q": ["kubernetes"], "type": ["video"],
                     "maxResults": ["5"]}
    assert call["headers"]["authorization"] == "Bearer tok"


def test_youtube_find_video_with_no_hits_is_a_verdict():
    transport = FakeTransport(("youtube/v3/search", 200, {"items": []}))

    output = run_youtube({"query": "nothing matches this"}, transport)

    assert output == {"found": False, "count": 0, "videos": [], "video": None}


def test_youtube_find_video_renders_the_query_template():
    transport = FakeTransport(("youtube/v3/search", 200, {"items": []}))

    run_youtube({"query": "{topic} kubernetes"}, transport)

    query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0]["url"]).query)
    assert query["q"] == ["kubernetes course kubernetes"]


def test_youtube_find_video_maps_provider_error_to_runtime_error():
    transport = FakeTransport(
        ("youtube/v3/search", 403,
         {"error": {"code": 403, "message": "The request is not properly authorized."}}))

    with pytest.raises(RuntimeError) as excinfo:
        run_youtube({"query": "kubernetes"}, transport)
    assert "HTTP 403" in str(excinfo.value)
    assert "not properly authorized" in str(excinfo.value)


def test_youtube_find_video_requires_a_query():
    transport = FakeTransport()

    with pytest.raises(ValueError):
        run_youtube({"query": "{missing_field}"}, transport)
    assert transport.calls == []


# --- youtube_find_playlist_items ----------------------------------------------


def run_youtube_playlist(action, transport):
    with with_connection("youtube", GOOGLE_CONNECTION):
        return run_youtube_find_playlist_items(
            {"type": "youtube_find_playlist_items", "connection_id": "yt-main", **action},
            EVENT, transport=transport)


def test_youtube_find_playlist_items_lists_the_playlist():
    transport = FakeTransport(
        ("playlistItems", 200,
         {"items": [
             {"snippet": {"title": "Episode 2", "publishedAt": "2026-09-02T00:00:00Z",
                          "channelTitle": "DTC", "resourceId": {"videoId": "abc2"}},
              "contentDetails": {"videoId": "abc2"}},
             {"snippet": {"title": "Episode 1", "publishedAt": "2026-09-01T00:00:00Z",
                          "resourceId": {"videoId": "abc1"}},
              "contentDetails": {"videoId": "abc1"}},
         ]}))

    output = run_youtube_playlist({"playlist_id": "PL-1"}, transport)

    assert output["found"] is True
    assert output["count"] == 2
    assert [video["id"] for video in output["videos"]] == ["abc2", "abc1"]
    assert output["video"]["title"] == "Episode 2"
    call = transport.calls[0]
    assert call["url"].startswith("https://www.googleapis.com/youtube/v3/playlistItems")
    assert call["headers"]["authorization"] == "Bearer tok"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
    assert query["playlistId"] == ["PL-1"]


def test_youtube_find_playlist_items_unknown_playlist_is_a_verdict():
    transport = FakeTransport(
        ("playlistItems", 404, {"error": {"message": "Playlist not found"}}))

    output = run_youtube_playlist({"playlist_id": "PL-gone"}, transport)

    assert output == {"found": False, "count": 0, "videos": [], "video": None}


def test_youtube_find_playlist_items_provider_error_raises():
    transport = FakeTransport(("playlistItems", 403, {"error": {"message": "Forbidden"}}))

    with pytest.raises(RuntimeError) as excinfo:
        run_youtube_playlist({"playlist_id": "PL-1"}, transport)
    assert "HTTP 403" in str(excinfo.value)
    assert "Forbidden" in str(excinfo.value)


def test_youtube_find_playlist_items_requires_a_playlist_id():
    transport = FakeTransport()

    with pytest.raises(ValueError):
        run_youtube_playlist({}, transport)
    assert transport.calls == []


# --- zoom_find_meeting --------------------------------------------------------


def run_zoom(action, transport):
    with with_connection("zoom", ZOOM_CONNECTION):
        return run_zoom_find_meeting(
            {"type": "zoom_find_meeting", "connection_id": "zoom-main", **action},
            EVENT, transport=transport)


def test_zoom_find_meeting_by_id():
    transport = FakeTransport(
        ("meetings/9001", 200,
         {"id": 9001, "topic": "Standup", "start_time": "2026-09-28T09:00:00Z",
          "join_url": "https://zoom.us/j/9001", "duration": 30}))

    output = run_zoom({"meeting_id": "{meeting_id}"}, transport)

    assert output == {"found": True, "meeting": {
        "id": "9001", "topic": "Standup", "start_time": "2026-09-28T09:00:00Z",
        "join_url": "https://zoom.us/j/9001", "duration": 30}}
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.zoom.us/v2/meetings/9001"
    assert call["headers"]["authorization"] == "Bearer tok"


def test_zoom_find_meeting_by_id_404_is_a_verdict():
    transport = FakeTransport(
        ("meetings/9001", 404, {"code": 3001, "message": "Meeting not found"}))

    output = run_zoom({"meeting_id": "9001"}, transport)

    assert output == {"found": False, "meeting": None}


def test_zoom_find_meeting_by_topic_contains_is_case_insensitive():
    transport = FakeTransport(
        ("users/me/meetings", 200,
         {"meetings": [
             {"id": 9002, "topic": "Design review", "start_time": "2026-09-29T15:00:00Z",
              "join_url": "https://zoom.us/j/9002", "duration": 60},
             {"id": 9003, "topic": "Kubernetes Course Live",
              "start_time": "2026-09-30T17:00:00Z",
              "join_url": "https://zoom.us/j/9003", "duration": 90},
         ]}))

    output = run_zoom({"topic": "kubernetes course"}, transport)

    assert output == {"found": True, "matched_by": "topic", "scope": "upcoming",
                      "meeting": {
        "id": "9003", "topic": "Kubernetes Course Live",
        "start_time": "2026-09-30T17:00:00Z",
        "join_url": "https://zoom.us/j/9003", "duration": 90}}
    query = urllib.parse.parse_qs(
        urllib.parse.urlsplit(transport.calls[0]["url"]).query)
    assert query["type"] == ["upcoming"]
    assert query["per_page"] == ["300"]


def test_zoom_find_meeting_exact_mode_requires_the_full_topic():
    transport = FakeTransport(("users/me/meetings", 200, {"meetings": [
        {"id": 9003, "topic": "Kubernetes Course Live"}]}))

    exact_miss = run_zoom({"topic": "kubernetes course", "match": "exact"}, transport)
    assert exact_miss == {"found": False, "meeting": None, "matched_by": "topic",
                          "scope": "upcoming"}

    exact_hit = run_zoom({"topic": "kubernetes course live", "match": "exact"},
                         transport)
    assert exact_hit["found"] is True
    assert exact_hit["meeting"]["id"] == "9003"


def test_zoom_find_meeting_topic_rendered_from_event():
    transport = FakeTransport(("users/me/meetings", 200, {"meetings": [
        {"id": 9003, "topic": "kubernetes course live"}]}))

    run_zoom({"topic": "{topic}"}, transport)

    # The rendered topic matched the meeting, so the listing was consulted.
    assert transport.calls[0]["url"].startswith("https://api.zoom.us/v2/users/me/meetings")


def test_zoom_find_meeting_provider_error_raises():
    transport = FakeTransport(
        ("users/me/meetings", 500, {"code": 500, "message": "Zoom is sad"}))

    with pytest.raises(RuntimeError) as excinfo:
        run_zoom({"topic": "kubernetes"}, transport)
    assert "HTTP 500" in str(excinfo.value)
    assert "Zoom is sad" in str(excinfo.value)


def test_zoom_find_meeting_requires_meeting_id_or_topic():
    transport = FakeTransport()

    with pytest.raises(ValueError):
        run_zoom({}, transport)
    assert transport.calls == []


# --- zoom_find_recording ------------------------------------------------------


def run_zoom_recording(action, transport):
    with with_connection("zoom", ZOOM_CONNECTION):
        return run_zoom_find_recording(
            {"type": "zoom_find_recording", "connection_id": "zoom-main", **action},
            EVENT, transport=transport)


def test_zoom_find_recording_by_meeting_id():
    transport = FakeTransport(
        ("meetings/9001/recordings", 200,
         {"id": 9001, "topic": "Standup", "start_time": "2026-09-28T09:00:00Z",
          "duration": 30,
          "recording_files": [
              {"id": "f-1", "file_type": "MP4", "file_size": 1024,
               "play_url": "https://zoom.us/rec/play/f-1",
               "download_url": "https://zoom.us/rec/download/f-1",
               "recording_start": "2026-09-28T09:00:00Z"}]}))

    output = run_zoom_recording({"meeting_id": "9001"}, transport)

    assert output["found"] is True
    assert output["count"] == 1
    assert output["recording"]["id"] == "9001"
    assert output["recording"]["files"][0]["download_url"] == "https://zoom.us/rec/download/f-1"
    call = transport.calls[0]
    assert call["url"] == "https://api.zoom.us/v2/meetings/9001/recordings"
    assert call["headers"]["authorization"] == "Bearer tok"


def test_zoom_find_recording_unknown_meeting_is_a_verdict():
    transport = FakeTransport(("meetings/9001/recordings", 404, {"code": 3001}))

    output = run_zoom_recording({"meeting_id": "9001"}, transport)

    assert output == {"found": False, "recording": None, "recordings": [], "count": 0}


def test_zoom_find_recording_by_topic_filters_the_30_day_listing():
    transport = FakeTransport(("users/me/recordings", 200, {"meetings": [
        {"id": 9002, "topic": "Design review", "recording_files": []},
        {"id": 9003, "topic": "Kubernetes Course Live",
         "recording_files": [{"id": "f-9", "file_type": "MP4"}]},
    ]}))

    output = run_zoom_recording({"topic": "kubernetes course"}, transport)

    assert output["found"] is True
    assert output["matched_by"] == "topic"
    assert output["recording"]["id"] == "9003"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0]["url"]).query)
    assert query["per_page"] == ["300"]
    assert "from" in query


def test_zoom_find_recording_without_arguments_returns_the_latest():
    transport = FakeTransport(("users/me/recordings", 200, {"meetings": [
        {"id": 9004, "topic": "Newest", "recording_files": [{"id": "f-4"}]},
        {"id": 9005, "topic": "Older", "recording_files": []},
    ]}))

    output = run_zoom_recording({}, transport)

    assert output["found"] is True
    assert output["recording"]["id"] == "9004"
    assert output["count"] == 2
    assert "matched_by" not in output


def test_zoom_find_recording_no_recordings_is_a_verdict():
    transport = FakeTransport(("users/me/recordings", 200, {"meetings": []}))

    output = run_zoom_recording({"topic": "anything"}, transport)

    assert output == {"found": False, "recording": None, "recordings": [],
                      "count": 0, "matched_by": "topic"}


def test_zoom_find_recording_provider_error_raises():
    transport = FakeTransport(("users/me/recordings", 500, {"code": 500, "message": "Zoom is sad"}))

    with pytest.raises(RuntimeError) as excinfo:
        run_zoom_recording({}, transport)
    assert "HTTP 500" in str(excinfo.value)
    assert "Zoom is sad" in str(excinfo.value)


def test_zoom_find_recording_rejects_an_unknown_match_mode():
    transport = FakeTransport()

    with pytest.raises(ValueError):
        run_zoom_recording({"match": "fuzzy"}, transport)
    assert transport.calls == []


# --- zoom_create_meeting ------------------------------------------------------


def run_zoom_create(action, transport):
    with with_connection("zoom", ZOOM_CONNECTION):
        return run_zoom_create_meeting(
            {"type": "zoom_create_meeting", "connection_id": "zoom-main", **action},
            EVENT, transport=transport)


def test_zoom_create_meeting_schedules_with_start_time():
    transport = FakeTransport(
        ("users/me/meetings", 201,
         {"id": 9100, "topic": "Live lecture", "start_time": "2026-10-01T09:00:00Z",
          "duration": 45, "timezone": "Europe/Berlin",
          "join_url": "https://zoom.us/j/9100", "start_url": "https://zoom.us/s/9100",
          "password": "secret1"}))

    output = run_zoom_create(
        {"topic": "Live lecture", "start_time": "2026-10-01T09:00:00Z",
         "duration": "45", "timezone": "Europe/Berlin", "agenda": "Week 1"}, transport)

    assert output == {"created": True, "scheduled": True, "meeting": {
        "id": "9100", "topic": "Live lecture", "start_time": "2026-10-01T09:00:00Z",
        "duration": 45, "join_url": "https://zoom.us/j/9100",
        "start_url": "https://zoom.us/s/9100", "passcode": "secret1"}}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.zoom.us/v2/users/me/meetings"
    assert call["headers"]["authorization"] == "Bearer tok"
    assert call["headers"]["content-type"] == "application/json"
    assert json.loads(call["body"]) == {
        "topic": "Live lecture", "type": 2, "start_time": "2026-10-01T09:00:00Z",
        "duration": 45, "timezone": "Europe/Berlin", "agenda": "Week 1"}


def test_zoom_create_meeting_without_start_time_is_instant():
    transport = FakeTransport(("users/me/meetings", 201, {"id": 9101, "topic": "Now"}))

    output = run_zoom_create({"topic": "Now"}, transport)

    assert output == {"created": True, "scheduled": False, "meeting": {
        "id": "9101", "topic": "Now", "start_time": None, "duration": None,
        "join_url": None, "start_url": None, "passcode": None}}
    assert json.loads(transport.calls[0]["body"]) == {"topic": "Now", "type": 1}


def test_zoom_create_meeting_defaults_scheduled_duration_to_60():
    transport = FakeTransport(("users/me/meetings", 201, {"id": 9102}))

    run_zoom_create({"topic": "Live", "start_time": "2026-10-01T09:00:00Z"}, transport)

    assert json.loads(transport.calls[0]["body"])["duration"] == 60


def test_zoom_create_meeting_renders_the_topic_template():
    transport = FakeTransport(("users/me/meetings", 201, {"id": 9103}))

    run_zoom_create({"topic": "{topic} live"}, transport)

    assert json.loads(transport.calls[0]["body"])["topic"] == "kubernetes course live"


def test_zoom_create_meeting_passes_settings_through_and_keeps_offsets():
    transport = FakeTransport(("users/me/meetings", 201, {"id": 9104}))

    run_zoom_create({"topic": "Live", "start_time": "2026-10-01T09:00:00+02:00",
                     "settings": '{{"join_before_host": true}}'}, transport)

    body = json.loads(transport.calls[0]["body"])
    assert body["settings"] == {"join_before_host": True}
    assert body["start_time"] == "2026-10-01T09:00:00+02:00"


def test_zoom_create_meeting_settings_take_templates_like_merge_fields():
    transport = FakeTransport(("users/me/meetings", 201, {"id": 9105}))

    run_zoom_create({"topic": "Live", "settings": '{"agenda": "{topic}"}'}, transport)

    assert json.loads(transport.calls[0]["body"])["settings"] == {
        "agenda": "kubernetes course"}


def test_zoom_create_meeting_rejects_non_object_settings():
    transport = FakeTransport()

    for bad in ("[1]", "not json"):
        with pytest.raises(ValueError):
            run_zoom_create({"topic": "Live", "settings": bad}, transport)
    assert transport.calls == []


def test_zoom_create_meeting_provider_error_raises():
    transport = FakeTransport(
        ("users/me/meetings", 400, {"code": 300, "message": "Invalid start time"}))

    with pytest.raises(RuntimeError) as excinfo:
        run_zoom_create({"topic": "Live", "start_time": "2026-10-01T09:00:00Z"},
                        transport)
    assert "HTTP 400" in str(excinfo.value)
    assert "Invalid start time" in str(excinfo.value)


def test_zoom_create_meeting_requires_a_topic():
    transport = FakeTransport()

    with pytest.raises(ValueError):
        run_zoom_create({"topic": "{missing_field}"}, transport)
    assert transport.calls == []


def test_zoom_create_meeting_rejects_a_bad_start_time():
    transport = FakeTransport()

    with pytest.raises(ValueError):
        run_zoom_create({"topic": "Live", "start_time": "tomorrow at 9"}, transport)
    assert transport.calls == []


def test_zoom_create_meeting_rejects_a_bad_duration():
    transport = FakeTransport()

    for duration in ("long", "0", "-5"):
        with pytest.raises(ValueError):
            run_zoom_create({"topic": "Live", "start_time": "2026-10-01T09:00:00Z",
                             "duration": duration}, transport)
    assert transport.calls == []


# --- telegram_find_chat -------------------------------------------------------


def run_telegram(action, transport):
    with with_connection("telegram", TELEGRAM_CONNECTION):
        return run_telegram_find_chat(
            {"type": "telegram_find_chat", "connection_id": "tg-bot", **action},
            EVENT, transport=transport)


def telegram_transport(result=None, *, ok=True, description=None):
    payload = {"ok": ok}
    if ok:
        payload["result"] = result
    else:
        payload["description"] = description
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=10):
        calls.append({"method": method, "url": url, "body": json.loads(body or b"{}")})
        return 200, json.dumps(payload).encode()

    transport.calls = calls
    return transport


def test_telegram_find_chat_returns_the_chat_profile():
    transport = telegram_transport(
        {"id": -10022, "title": "DataTalksClub Announcements", "username": "dtc_announce",
         "type": "channel"})

    output = run_telegram({"chat_id": "{chat_handle}"}, transport)

    assert output == {"found": True, "chat": {
        "id": -10022, "title": "DataTalksClub Announcements",
        "username": "dtc_announce", "type": "channel"}}
    call = transport.calls[0]
    assert call["url"].endswith("/getChat")
    assert call["body"] == {"chat_id": "@dtc_announce"}


def test_telegram_find_chat_unknown_chat_is_a_verdict():
    transport = telegram_transport(ok=False, description="Bad Request: chat not found")

    output = run_telegram({"chat_id": "@gone"}, transport)

    assert output == {"found": False, "chat": None}


def test_telegram_find_chat_other_telegram_errors_raise():
    transport = telegram_transport(ok=False, description="Unauthorized")

    with pytest.raises(telegram_api.TelegramApiError) as excinfo:
        run_telegram({"chat_id": "@gone"}, transport)
    assert "Unauthorized" in str(excinfo.value)


def test_telegram_find_chat_requires_a_chat_id():
    transport = telegram_transport({"id": 1})

    with pytest.raises(ValueError):
        run_telegram({"chat_id": "{missing_field}"}, transport)
    assert transport.calls == []


# --- registry wiring ----------------------------------------------------------


def test_registry_exposes_the_three_find_actions():
    specs = registry.action_specs()
    assert specs["youtube_find_video"] == ({"connection_id", "query"}, set())
    assert specs["zoom_find_meeting"] == (
        {"connection_id"},
        {"meeting_id", "topic", "match", "scope", "create_if_missing",
         "start_time", "duration", "timezone", "agenda", "settings"})
    assert specs["telegram_find_chat"] == ({"connection_id", "chat_id"}, set())


def test_registry_exposes_the_recording_and_playlist_actions():
    specs = registry.action_specs()
    assert specs["zoom_find_recording"] == (
        {"connection_id"}, {"meeting_id", "topic", "match"})
    assert specs["youtube_find_playlist_items"] == (
        {"connection_id", "playlist_id"}, set())
    recording = registry.ACTIONS["zoom_find_recording"]
    meeting_field = next(f for f in recording.fields if f["key"] == "meeting_id")
    assert meeting_field["discover"] == {"resource": "zoom.recordings"}
    playlist = registry.ACTIONS["youtube_find_playlist_items"]
    playlist_field = next(f for f in playlist.fields if f["key"] == "playlist_id")
    assert playlist_field["discover"] == {"resource": "youtube.playlists"}


def test_find_action_fields_carry_the_picker_hints():
    youtube = registry.ACTIONS["youtube_find_video"]
    assert [field["key"] for field in youtube.fields] == ["connection_id", "query"]
    zoom = registry.ACTIONS["zoom_find_meeting"]
    meeting_field = next(f for f in zoom.fields if f["key"] == "meeting_id")
    assert meeting_field["discover"] == {"resource": "zoom.meetings"}
    match_field = next(f for f in zoom.fields if f["key"] == "match")
    assert match_field["options"] == ["contains", "exact"]
    assert match_field["default"] == "contains"
    telegram = registry.ACTIONS["telegram_find_chat"]
    chat_field = next(f for f in telegram.fields if f["key"] == "chat_id")
    assert chat_field["discover"] == {"resource": "telegram.chats"}
    assert chat_field["placeholder"] == "@channel or -100…"


def test_find_actions_validate_against_the_registry_chain():
    registry.validate_action_chain([
        {"type": "youtube_find_video", "connection_id": "yt", "query": "{topic}"},
        {"type": "zoom_find_meeting", "connection_id": "zoom", "topic": "standup"},
        {"type": "telegram_find_chat", "connection_id": "tg", "chat_id": "@dtc"},
        {"type": "zoom_find_recording", "connection_id": "zoom"},
        {"type": "youtube_find_playlist_items", "connection_id": "yt",
         "playlist_id": "{steps.pick.output.playlist_id}"},
    ])
    with pytest.raises(registry.ActionError):
        registry.validate_action_chain([
            {"type": "youtube_find_video", "connection_id": "yt", "query": "k8s",
             "channel": "UCnope"}  # unknown key
        ])


def test_registry_exposes_the_zoom_create_action():
    specs = registry.action_specs()
    assert specs["zoom_create_meeting"] == (
        {"connection_id", "topic"},
        {"start_time", "duration", "timezone", "agenda", "settings"})
    create = registry.ACTIONS["zoom_create_meeting"]
    assert [field["key"] for field in create.fields] == [
        "connection_id", "topic", "start_time", "duration", "timezone",
        "agenda", "settings"]
    registry.validate_action_chain([
        {"type": "zoom_create_meeting", "connection_id": "zoom", "topic": "Live",
         "start_time": "2026-10-01T09:00:00Z"}])
    with pytest.raises(registry.ActionError):
        registry.validate_action_chain([
            {"type": "zoom_create_meeting", "connection_id": "zoom"}  # no topic
        ])
