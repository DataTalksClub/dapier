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
from src.dapier.engine.actions.telegram import run_telegram_find_chat
from src.dapier.engine.actions.youtube import run_youtube_find_video
from src.dapier.engine.actions.zoom import run_zoom_find_meeting


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

    assert output == {"found": True, "matched_by": "topic", "meeting": {
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
    assert exact_miss == {"found": False, "meeting": None, "matched_by": "topic"}

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
        {"connection_id"}, {"meeting_id", "topic", "match"})
    assert specs["telegram_find_chat"] == ({"connection_id", "chat_id"}, set())


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
    ])
    with pytest.raises(registry.ActionError):
        registry.validate_action_chain([
            {"type": "youtube_find_video", "connection_id": "yt", "query": "k8s",
             "channel": "UCnope"}  # unknown key
        ])
