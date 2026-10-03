"""Discovery contract tests: Zoom meetings, recordings, and the health check.

Unit tests drive the registered Discovery run callables with a fake
transport; API tests go through ``api.discovery`` — the domain layer both
/api/agent/* and /api/admin/* dispatch to — with the provider HTTP seam
monkeypatched, proving the console and the CLI reach the same behavior.
"""

import json
import urllib.parse

import pytest

from src.dapier.api import discovery as api_discovery
from src.dapier.api import runs
from src.dapier.connectors import registry
from src.dapier.connectors import trigger_discovery
import plugins.zoom.connector as zoom_connector  # noqa: F401 (registers)
from src.dapier.connections import discovery as provider
from src.dapier.connections import tokens
from src.dapier.connections.providers import oauth_providers

CONNECTION = {"connection_id": "zoom-main", "provider": "zoom", "status": "connected",
              "credential_id": "oauth#zoom-main"}

MEETINGS_PAGE = {"meetings": [
    {"id": 9001, "topic": "Standup", "start_time": "2026-09-28T09:00:00Z",
     "join_url": "https://zoom.us/j/9001"},
    {"id": 9002, "topic": "Design review", "start_time": "2026-09-29T15:00:00Z",
     "join_url": "https://zoom.us/j/9002"},
]}
RECORDINGS_PAGE = {"meetings": [
    {"id": 8801, "topic": "Retro", "start_time": "2026-09-20T10:00:00Z",
     "recording_files": [{"file_type": "MP4"}, {"file_type": "M4A"}]},
]}
ACCOUNT = {"id": "u-123", "first_name": "Ada", "last_name": "Byron",
           "email": "ada@example.com"}


@pytest.fixture(autouse=True)
def live_token(monkeypatch):
    monkeypatch.setattr(
        tokens, "get_access_token",
        lambda connection, transport=None: ("tok", {}))


def recording_transport(responses):
    """A transport serving one canned JSON response per call, recording each."""
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url, "headers": headers})
        return 200, json.dumps(responses[len(calls) - 1]).encode()

    transport.calls = calls
    return transport


def discovery(name):
    return registry.DISCOVERIES[f"zoom.{name}"]


# --- registry and domain: the resources a Zoom connection can discover ---


def test_zoom_exposes_meetings_past_meetings_recordings_and_webinars():
    entries = registry.discoveries_for_provider("zoom")
    assert [entry.name for entry in entries] == \
        ["meetings", "past_meetings", "recordings", "webinars"]
    for entry in entries:
        assert entry.label and entry.description
        assert entry.params == ()


def test_meetings_list_upcoming_meetings():
    transport = recording_transport([MEETINGS_PAGE])

    items = discovery("meetings").run(CONNECTION, {}, transport=transport)

    assert items == [
        {"id": "9001", "name": "Standup", "start_time": "2026-09-28T09:00:00Z",
         "join_url": "https://zoom.us/j/9001"},
        {"id": "9002", "name": "Design review", "start_time": "2026-09-29T15:00:00Z",
         "join_url": "https://zoom.us/j/9002"},
    ]
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"].startswith("https://api.zoom.us/v2/users/me/meetings")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)
    assert query["type"] == ["upcoming"]
    assert query["per_page"] == ["25"]
    assert call["headers"]["authorization"] == "Bearer tok"


def test_recordings_look_back_thirty_days():
    transport = recording_transport([RECORDINGS_PAGE])

    items = discovery("recordings").run(CONNECTION, {}, transport=transport)

    assert items == [{"id": "8801", "name": "Retro",
                      "start_time": "2026-09-20T10:00:00Z", "recording_count": 2}]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(transport.calls[0]["url"]).query)
    assert query["per_page"] == ["25"]
    assert "from" in query


def test_unknown_resource_names_the_known_ones():
    with pytest.raises(provider.DiscoveryError) as excinfo:
        provider.discover(CONNECTION, "polls", {})
    assert excinfo.value.status == 404
    assert "known: meetings, past_meetings, recordings, webinars" in str(excinfo.value)


def test_zoom_health_check_verifies_the_account_profile():
    def transport(method, url, *, headers=None, body=None, timeout=15):
        assert url == "https://zoom.us/v2/users/me"
        assert headers["authorization"] == "Bearer tok"
        return 200, json.dumps(ACCOUNT).encode()

    account_id, name = oauth_providers.verify_account("zoom", "tok", transport=transport)

    assert account_id == "u-123"
    assert name == "Ada Byron"


# --- API surface: api.discovery, shared by /api/agent/* and /api/admin/* ---


class Table:
    def __init__(self, items):
        self.items = items

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"].get("connection_id"))
        return {"Item": dict(item)} if item else {}


def patch_provider_transport(monkeypatch, responses):
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"url": url})
        return 200, json.dumps(responses[len(calls) - 1]).encode()

    monkeypatch.setattr(provider, "_default_transport", transport)
    return calls


def test_api_resources_render_picker_metadata():
    table = Table({"zoom-main": CONNECTION})

    status, payload = api_discovery.resources("zoom-main", connections_table=table)

    assert status == 200
    assert payload["provider"] == "zoom"
    assert [resource["name"] for resource in payload["resources"]] == \
        ["meetings", "past_meetings", "recordings", "webinars"]


def test_api_discover_runs_meetings_end_to_end(monkeypatch):
    calls = patch_provider_transport(monkeypatch, [MEETINGS_PAGE])
    table = Table({"zoom-main": CONNECTION})

    status, payload = api_discovery.discover(
        "zoom-main", "meetings", {}, connections_table=table)

    assert status == 200
    assert payload["resource"] == "meetings"
    assert payload["items"][0] == {"id": "9001", "name": "Standup",
                                   "start_time": "2026-09-28T09:00:00Z",
                                   "join_url": "https://zoom.us/j/9001"}
    assert calls[0]["url"].startswith("https://api.zoom.us/v2/users/me/meetings")


def test_api_discover_runs_recordings_end_to_end(monkeypatch):
    patch_provider_transport(monkeypatch, [RECORDINGS_PAGE])
    table = Table({"zoom-main": CONNECTION})

    status, payload = api_discovery.discover(
        "zoom-main", "recordings", {}, connections_table=table)

    assert status == 200
    assert payload["items"] == [{"id": "8801", "name": "Retro",
                                 "start_time": "2026-09-20T10:00:00Z",
                                 "recording_count": 2}]


def test_api_discover_accepts_the_full_connector_name(monkeypatch):
    patch_provider_transport(monkeypatch, [MEETINGS_PAGE])
    table = Table({"zoom-main": CONNECTION})

    status, payload = api_discovery.discover(
        "zoom-main", "zoom.meetings", {}, connections_table=table)

    assert status == 200
    assert payload["resource"] == "meetings"


def test_api_discover_unknown_resource_names_the_known_ones():
    table = Table({"zoom-main": CONNECTION})

    status, payload = api_discovery.discover(
        "zoom-main", "polls", {}, connections_table=table)

    assert status == 404
    assert "known: meetings, past_meetings, recordings, webinars" in payload["error"]


def test_api_discover_requires_a_connected_connection():
    table = Table({"zoom-main": dict(CONNECTION, status="pending")})

    status, payload = api_discovery.discover(
        "zoom-main", "meetings", {}, connections_table=table)

    assert status == 400
    assert "not connected" in payload["error"]


def test_api_connection_test_reports_the_zoom_identity(monkeypatch):
    def transport(method, url, *, headers=None, body=None, timeout=15):
        assert url == "https://zoom.us/v2/users/me"
        return 200, json.dumps(ACCOUNT).encode()

    monkeypatch.setattr(oauth_providers, "_default_transport", transport)
    table = Table({"zoom-main": CONNECTION})

    status, payload = api_discovery.test_connection(
        "zoom-main", connections_table=table)

    assert status == 200
    assert payload["ok"] is True
    assert payload["identity"] == {"id": "u-123", "name": "Ada Byron"}


# --- trigger samples: one documented payload per declared event -----------------
#
# connectors.zoom registers a sample TriggerDiscovery backed by
# per_event_sample_fetch: the request's ``event`` field picks the payload,
# and recorded history fills the sample only when its replayed envelope
# carries the asked event (a started run is never renamed to ended).


@pytest.fixture(autouse=True)
def no_recorded_runs(monkeypatch):
    """The sample chain starts at synthetic: no recorded runs, by default."""
    monkeypatch.setattr(runs, "recent", lambda *args, **kwargs: [])


def _trigger_sample(**body):
    status, payload = trigger_discovery.api_discover({"connector": "zoom", **body})
    assert status == 200, payload
    return payload


def test_trigger_sample_meeting_started_serves_the_meeting_payload():
    payload = _trigger_sample(event="meeting.started")

    assert payload["connector"] == "zoom"
    assert payload["source"] == "synthetic"
    assert payload["event"] == "meeting.started"
    assert payload["sample"]["event"] == "meeting.started"
    data = payload["sample"]["data"]
    assert data["uuid"] and data["id"] and data["topic"] and data["start_time"]
    assert "end_time" not in data
    assert "download_token" not in json.dumps(payload["sample"])


def test_trigger_sample_serves_each_declared_event():
    recording = _trigger_sample(event="recording.completed")["sample"]
    transcript = _trigger_sample(event="recording.transcript_completed")["sample"]
    ended = _trigger_sample(event="meeting.ended")["sample"]

    assert recording["event"] == "recording.completed"
    assert [file["file_type"] for file in recording["data"]["video_files"]] == ["MP4"]
    assert transcript["event"] == "recording.transcript_completed"
    assert [file["file_type"] for file in transcript["data"]["video_files"]] == \
        ["MP4", "TRANSCRIPT"]
    assert ended["event"] == "meeting.ended"
    assert ended["data"]["end_time"]
    assert "end_time" not in _trigger_sample(event="meeting.started")["sample"]["data"]


def test_trigger_sample_unknown_event_falls_back_to_the_recording_example():
    payload = _trigger_sample(event="meeting.participant_joined")

    assert payload["source"] == "synthetic"
    assert payload["event"] == "recording.completed"
    assert payload["sample"]["data"]["video_files"]


def test_trigger_sample_history_only_fills_the_matching_event(monkeypatch):
    """A recorded meeting.started run fills a meeting.started ask — only that."""
    envelope = {"id": "zoom:abc123", "connector": "zoom", "event": "meeting.started",
                "source": "zoom", "occurred_at": "2026-09-28T09:00:00+00:00",
                "data": {"account_id": "acct-1", "uuid": "u-1", "id": 123,
                         "topic": "Standup", "host_id": "host-1",
                         "start_time": "2026-09-28T09:00:00Z", "duration": 45,
                         "timezone": "Europe/Berlin"}}
    monkeypatch.setattr(runs, "recent", lambda *args, **kwargs: [
        {"run_id": "standup:evt-1", "connector": "zoom"}])
    monkeypatch.setattr(runs, "api_get", lambda run_id: (200, {"steps": []}))
    monkeypatch.setattr(runs, "replay_event", lambda run_id, steps: (envelope, None))

    started = _trigger_sample(event="meeting.started")
    assert started["source"] == "history"
    assert started["event"] == "meeting.started"
    assert started["sample"]["data"] == envelope["data"]
    assert started["sample"]["id"] == envelope["id"]

    ended = _trigger_sample(event="meeting.ended")
    assert ended["source"] == "synthetic"  # history is never renamed
    assert ended["event"] == "meeting.ended"
