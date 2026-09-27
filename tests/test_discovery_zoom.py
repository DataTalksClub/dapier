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
from src.dapier.connectors import registry
from src.dapier.connectors import zoom as zoom_connector  # noqa: F401 (registers)
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


def test_zoom_exposes_meetings_and_recordings():
    entries = registry.discoveries_for_provider("zoom")
    assert [entry.name for entry in entries] == ["meetings", "recordings"]
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
        provider.discover(CONNECTION, "webinars", {})
    assert excinfo.value.status == 404
    assert "known: meetings, recordings" in str(excinfo.value)


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
        ["meetings", "recordings"]


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
        "zoom-main", "webinars", {}, connections_table=table)

    assert status == 404
    assert "known: meetings, recordings" in payload["error"]


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
