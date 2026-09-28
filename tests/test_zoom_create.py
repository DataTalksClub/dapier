"""zoom_create_meeting: the connector's create action (Zapier's top Zoom move).

Unit tests drive the runner with a fake provider transport (the
test_find_media FakeTransport pattern) plus the connection/token seams
monkeypatched the same way, check the registry spec, and run one dispatch
through the engine's registry.run_action path.
"""

import json
import unittest.mock as mock

import pytest

from src.dapier.connectors import registry
from src.dapier.connections import credentials as credentials_module
from src.dapier.connections import tokens
from src.dapier.engine.actions import base as actions_base
from src.dapier.engine.actions.zoom import run_zoom_create_meeting


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


ZOOM_CONNECTION = {"connection_id": "zoom-main", "provider": "zoom",
                   "status": "connected", "credential_id": "oauth#zoom-main"}

EVENT = {"connector": "zoom", "event": "meeting.started",
         "occurred_at": "2026-09-27T10:00:00+00:00",
         "data": {"topic": "kubernetes course live",
                  "start_time": "2026-10-01T09:00:00Z"}}

CREATED = {"id": 9501, "topic": "Weekly sync",
           "start_time": "2026-10-01T09:00:00Z", "duration": 60,
           "join_url": "https://zoom.us/j/9501",
           "start_url": "https://zoom.us/s/9501", "password": "secret-pass"}


@pytest.fixture(autouse=True)
def connections(monkeypatch):
    """Every connection resolves live and every token is a stored secret."""
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))
    monkeypatch.setattr(credentials_module, "get_credential",
                        lambda credential_id: {"token": "tok"})


def run_create(action, transport):
    with mock.patch("src.dapier.engine.actions.zoom._zoom_connection",
                    return_value=dict(ZOOM_CONNECTION)):
        return run_zoom_create_meeting(
            {"type": "zoom_create_meeting", "connection_id": "zoom-main", **action},
            EVENT, transport=transport)


# --- happy path ----------------------------------------------------------------


def test_create_scheduled_meeting_posts_the_zoom_payload():
    transport = FakeTransport(("users/me/meetings", 201, CREATED))

    output = run_create({"topic": "Weekly sync",
                         "start_time": "2026-10-01T09:00:00Z",
                         "duration": "60", "timezone": "Europe/Berlin",
                         "agenda": "Roadmap review"}, transport)

    assert output == {"created": True, "scheduled": True, "meeting": {
        "id": "9501", "topic": "Weekly sync",
        "start_time": "2026-10-01T09:00:00Z", "duration": 60,
        "join_url": "https://zoom.us/j/9501",
        "start_url": "https://zoom.us/s/9501", "passcode": "secret-pass"}}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.zoom.us/v2/users/me/meetings"
    assert call["headers"]["authorization"] == "Bearer tok"
    assert call["headers"]["content-type"] == "application/json"
    assert json.loads(call["body"]) == {
        "topic": "Weekly sync", "type": 2, "start_time": "2026-10-01T09:00:00Z",
        "duration": 60, "timezone": "Europe/Berlin", "agenda": "Roadmap review"}


def test_create_meeting_renders_templates_from_the_event():
    transport = FakeTransport(("users/me/meetings", 201, CREATED))

    run_create({"topic": "{topic}", "start_time": "{start_time}"}, transport)

    body = json.loads(transport.calls[0]["body"])
    assert body["topic"] == "kubernetes course live"
    assert body["start_time"] == "2026-10-01T09:00:00Z"
    assert body["duration"] == 60  # scheduled meetings default to one hour


def test_create_meeting_without_start_time_is_an_instant_meeting():
    transport = FakeTransport(("users/me/meetings", 201, CREATED))

    output = run_create({"topic": "Ops huddle"}, transport)

    assert output["created"] is True
    assert output["scheduled"] is False
    body = json.loads(transport.calls[0]["body"])
    assert body == {"topic": "Ops huddle", "type": 1}
    assert "start_time" not in body and "duration" not in body


def test_create_meeting_passes_zoom_settings_through_verbatim():
    transport = FakeTransport(("users/me/meetings", 201, CREATED))

    run_create({"topic": "Weekly sync", "start_time": "2026-10-01T09:00:00Z",
                "settings": '{{"join_before_host": true, "waiting_room": false}}'},
               transport)

    body = json.loads(transport.calls[0]["body"])
    assert body["settings"] == {"join_before_host": True, "waiting_room": False}


def test_create_meeting_settings_render_merge_fields_around_json_braces():
    transport = FakeTransport(("users/me/meetings", 201, CREATED))

    run_create({"topic": "Weekly sync",
                "settings": '{"agenda": "{topic}", "default_password": false}'},
               transport)

    body = json.loads(transport.calls[0]["body"])
    assert body["settings"] == {"agenda": "kubernetes course live",
                                "default_password": False}


# --- clean failures -------------------------------------------------------------


def test_create_meeting_requires_a_topic():
    transport = FakeTransport()

    with pytest.raises(ValueError, match="requires topic"):
        run_create({"start_time": "2026-10-01T09:00:00Z"}, transport)
    assert transport.calls == []


def test_create_meeting_rejects_a_non_iso_start_time():
    transport = FakeTransport()

    with pytest.raises(ValueError, match="ISO 8601"):
        run_create({"topic": "Weekly sync", "start_time": "tomorrow morning"},
                   transport)
    assert transport.calls == []


@pytest.mark.parametrize("duration", ["soon", "0", "-15"])
def test_create_meeting_rejects_a_bad_duration(duration):
    transport = FakeTransport()

    with pytest.raises(ValueError, match="duration"):
        run_create({"topic": "Weekly sync",
                    "start_time": "2026-10-01T09:00:00Z",
                    "duration": duration}, transport)
    assert transport.calls == []


def test_create_meeting_rejects_non_object_settings():
    transport = FakeTransport()

    with pytest.raises(ValueError, match="settings must be a JSON object"):
        run_create({"topic": "Weekly sync", "settings": "true"}, transport)
    assert transport.calls == []


def test_create_meeting_maps_zoom_errors_to_runtime_errors():
    transport = FakeTransport(
        ("users/me/meetings", 403, {"code": 403, "message": "Forbidden"}))

    with pytest.raises(RuntimeError) as excinfo:
        run_create({"topic": "Weekly sync",
                    "start_time": "2026-10-01T09:00:00Z"}, transport)
    assert "HTTP 403" in str(excinfo.value)
    assert "Forbidden" in str(excinfo.value)


# --- registry wiring ------------------------------------------------------------


def test_registry_exposes_the_create_action_spec():
    assert registry.action_specs()["zoom_create_meeting"] == (
        {"connection_id", "topic"},
        {"start_time", "duration", "timezone", "agenda", "settings"})


def test_create_action_chains_validate_against_the_registry():
    registry.validate_action_chain([
        {"type": "zoom_create_meeting", "connection_id": "zoom",
         "topic": "{topic}", "start_time": "2026-10-01T09:00:00Z",
         "duration": 45, "timezone": "Europe/Berlin", "agenda": "Roadmap",
         "settings": "{{\"join_before_host\": true}}"},
    ])
    with pytest.raises(registry.ActionError):
        registry.validate_action_chain([
            {"type": "zoom_create_meeting", "connection_id": "zoom",
             "topic": "Weekly sync", "meeting_id": "9501"}  # unknown key
        ])
    with pytest.raises(registry.ActionError):
        registry.validate_action_chain([
            {"type": "zoom_create_meeting", "connection_id": "zoom",
             "topic": "Weekly sync", "duration": "sixty"}  # not a number
        ])


# --- engine run path ------------------------------------------------------------


def test_create_meeting_runs_through_the_engine_dispatch():
    """registry.run_action drives the registered lambda with the default
    transport — patch the seams it resolves and dispatch end to end."""
    transport = FakeTransport(("users/me/meetings", 201, CREATED))
    with mock.patch("src.dapier.engine.actions.base._connected_connection",
                    return_value=dict(ZOOM_CONNECTION)), \
            mock.patch("src.dapier.engine.actions.base._default_transport",
                       transport):
        output = registry.run_action(
            {"type": "zoom_create_meeting", "connection_id": "zoom-main",
             "topic": "Weekly sync", "start_time": "2026-10-01T09:00:00Z",
             "duration": "60"},
            EVENT, "wf-1")

    assert output == {"created": True, "scheduled": True, "meeting": {
        "id": "9501", "topic": "Weekly sync",
        "start_time": "2026-10-01T09:00:00Z", "duration": 60,
        "join_url": "https://zoom.us/j/9501",
        "start_url": "https://zoom.us/s/9501", "passcode": "secret-pass"}}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.zoom.us/v2/users/me/meetings"
    assert json.loads(call["body"])["topic"] == "Weekly sync"
