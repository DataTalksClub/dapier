"""Action-breadth tests for the Zapier-parity lanes: zoom_create_meeting
and dropbox_get_temp_link.

Unit tests drive each registered runner with a fake provider transport and
the connection/token seams patched (the test_find_media /
test_find_slack_dropbox pattern), asserting method, URL, request body and
the step-output shape. The registry half checks that the new types are
registered, that validate_action_chain accepts a valid chain and rejects
missing required fields and unknown keys, and that save-time field typing
rejects a non-numeric zoom duration literal.
"""
import json
import unittest.mock as mock

import pytest

from src.dapier.connectors import registry
from src.dapier.connections import tokens
from src.dapier.engine.actions.dropbox import run_dropbox_get_temp_link
from src.dapier.engine.actions.zoom import run_zoom_create_meeting

import src.dapier.connectors  # noqa: F401  (import = registration)


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
DROPBOX_CONNECTION = {"connection_id": "dbx", "provider": "dropbox",
                      "status": "connected", "credential_id": "oauth#dbx",
                      "root_path": "/team/shared"}

EVENT = {"connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"topic": "Live lecture",
                  "path": "/Invoices/invoice-4137.pdf"}}


@pytest.fixture(autouse=True)
def token_seam(monkeypatch):
    """Every connection's access token resolves to a stored secret."""
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))


# --- zoom_create_meeting ------------------------------------------------------


def run_zoom_create(action, transport):
    with mock.patch("src.dapier.engine.actions.zoom._zoom_connection",
                    return_value=ZOOM_CONNECTION):
        return run_zoom_create_meeting(
            {"type": "zoom_create_meeting", "connection_id": "zoom-main", **action},
            EVENT, transport=transport)


def test_zoom_create_meeting_schedules_a_meeting():
    transport = FakeTransport(
        ("users/me/meetings", 201,
         {"id": 9100, "topic": "Live lecture", "start_time": "2026-10-01T09:00:00Z",
          "duration": 45, "join_url": "https://zoom.us/j/9100",
          "start_url": "https://zoom.us/s/9100", "password": "secret1"}))

    output = run_zoom_create(
        {"topic": "{topic}", "start_time": "2026-10-01T09:00:00Z",
         "duration": "45", "timezone": "Europe/Berlin", "agenda": "Week 1"},
        transport)

    assert output == {"created": True, "scheduled": True, "meeting": {
        "id": "9100", "topic": "Live lecture",
        "start_time": "2026-10-01T09:00:00Z", "duration": 45,
        "join_url": "https://zoom.us/j/9100",
        "start_url": "https://zoom.us/s/9100", "passcode": "secret1"}}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.zoom.us/v2/users/me/meetings"
    assert call["headers"]["authorization"] == "Bearer tok"
    assert call["headers"]["content-type"] == "application/json"
    assert json.loads(call["body"]) == {
        "topic": "Live lecture", "type": 2,
        "start_time": "2026-10-01T09:00:00Z", "duration": 45,
        "timezone": "Europe/Berlin", "agenda": "Week 1"}


def test_zoom_create_meeting_without_start_time_is_instant():
    transport = FakeTransport(
        ("users/me/meetings", 201,
         {"id": 9101, "topic": "Now", "join_url": "https://zoom.us/j/9101"}))

    output = run_zoom_create({"topic": "Now"}, transport)

    assert output["created"] is True
    assert output["scheduled"] is False
    assert output["meeting"]["id"] == "9101"
    assert output["meeting"]["join_url"] == "https://zoom.us/j/9101"
    assert json.loads(transport.calls[0]["body"]) == {"topic": "Now", "type": 1}


def test_zoom_create_meeting_provider_error_raises():
    transport = FakeTransport(
        ("users/me/meetings", 400, {"code": 300, "message": "Invalid start time"}))

    with pytest.raises(RuntimeError) as excinfo:
        run_zoom_create({"topic": "Live", "start_time": "2026-10-01T09:00:00Z"},
                        transport)
    assert "HTTP 400" in str(excinfo.value)
    assert "Invalid start time" in str(excinfo.value)


def test_zoom_create_meeting_dispatches_through_the_registry():
    transport = FakeTransport(
        ("users/me/meetings", 201,
         {"id": 9110, "topic": "Live lecture", "join_url": "https://zoom.us/j/9110"}))
    with mock.patch("src.dapier.engine.actions.zoom._zoom_connection",
                    return_value=ZOOM_CONNECTION), \
         mock.patch("src.dapier.engine.actions.base._default_transport", transport):
        output = registry.ACTIONS["zoom_create_meeting"].run(
            {"type": "zoom_create_meeting", "connection_id": "zoom-main",
             "topic": "{topic}"}, EVENT, "wf-1")

    assert output["created"] is True
    assert output["meeting"]["id"] == "9110"
    assert output["meeting"]["join_url"] == "https://zoom.us/j/9110"
    assert json.loads(transport.calls[0]["body"])["topic"] == "Live lecture"


# --- dropbox_get_temp_link ------------------------------------------------------


def run_temp_link(action, transport):
    with mock.patch("src.dapier.engine.actions.dropbox._dropbox_connection",
                    return_value=DROPBOX_CONNECTION):
        return run_dropbox_get_temp_link(
            {"type": "dropbox_get_temp_link", "connection_id": "dbx", **action},
            EVENT, transport=transport)


def test_dropbox_get_temp_link_posts_the_path_and_returns_the_link():
    link = "https://uc1a2b3c.dl.dropboxusercontent.com/invoice-4137.pdf"
    metadata = {"id": "id:f1", "name": "invoice-4137.pdf", ".tag": "file",
                "path_display": "/Invoices/invoice-4137.pdf", "size": 51200}
    transport = FakeTransport(
        ("get_temporary_link", 200, {"link": link, "metadata": metadata}))

    output = run_temp_link({"path": "/Invoices/invoice-4137.pdf"}, transport)

    assert output == {"link": link, "item": {
        "id": "id:f1", "name": "invoice-4137.pdf", "tag": "file",
        "path": "/Invoices/invoice-4137.pdf", "size": 51200, "modified": ""}}
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.dropboxapi.com/2/files/get_temporary_link"
    assert call["headers"]["authorization"] == "Bearer tok"
    assert call["headers"]["content-type"] == "application/json"
    assert json.loads(call["body"]) == {"path": "/Invoices/invoice-4137.pdf"}


def test_dropbox_get_temp_link_renders_the_path_template():
    transport = FakeTransport(
        ("get_temporary_link", 200,
         {"link": "https://uc1a2b3c.dl.dropboxusercontent.com/invoice-4137.pdf",
          "metadata": {"id": "id:f1"}}))

    output = run_temp_link({"path": "{path}"}, transport)

    assert output["link"].endswith("invoice-4137.pdf")
    assert output["item"]["id"] == "id:f1"
    assert json.loads(
        transport.calls[0]["body"])["path"] == "/Invoices/invoice-4137.pdf"


def test_dropbox_get_temp_link_without_a_path_uses_the_event_file_path():
    transport = FakeTransport(
        ("get_temporary_link", 200,
         {"link": "https://uc1a2b3c.dl.dropboxusercontent.com/invoice-4137.pdf",
          "metadata": {"id": "id:f1"}}))

    output = run_temp_link({}, transport)

    assert output["link"].endswith("invoice-4137.pdf")
    assert json.loads(
        transport.calls[0]["body"])["path"] == "/Invoices/invoice-4137.pdf"


def test_dropbox_get_temp_link_without_metadata_still_returns_the_link():
    transport = FakeTransport(
        ("get_temporary_link", 200,
         {"link": "https://uc1a2b3c.dl.dropboxusercontent.com/f.pdf"}))

    output = run_temp_link({"path": "/f.pdf"}, transport)

    assert output == {"link": "https://uc1a2b3c.dl.dropboxusercontent.com/f.pdf",
                      "item": None}


def test_dropbox_get_temp_link_requires_a_path():
    transport = FakeTransport()

    with pytest.raises(ValueError):
        with mock.patch("src.dapier.engine.actions.dropbox._dropbox_connection",
                        return_value=DROPBOX_CONNECTION):
            run_dropbox_get_temp_link(
                {"type": "dropbox_get_temp_link", "connection_id": "dbx",
                 "path": "  "}, {"data": {}}, transport=transport)
    assert transport.calls == []


def test_dropbox_get_temp_link_provider_error_raises():
    transport = FakeTransport(
        ("get_temporary_link", 409,
         {"error": {".tag": "path", "path": {".tag": "not_found"}}}))

    with pytest.raises(RuntimeError) as excinfo:
        run_temp_link({"path": "/gone.pdf"}, transport)
    assert "HTTP 409" in str(excinfo.value)


def test_dropbox_get_temp_link_dispatches_through_the_registry():
    transport = FakeTransport(
        ("get_temporary_link", 200,
         {"link": "https://uc1a2b3c.dl.dropboxusercontent.com/f.pdf",
          "metadata": {".tag": "file"}}))
    with mock.patch("src.dapier.engine.actions.base._connected_connection",
                    return_value=DROPBOX_CONNECTION), \
         mock.patch("src.dapier.engine.actions.base._default_transport", transport):
        output = registry.ACTIONS["dropbox_get_temp_link"].run(
            {"type": "dropbox_get_temp_link", "connection_id": "dbx",
             "path": "{path}"}, EVENT, "wf-1")

    assert output["link"] == "https://uc1a2b3c.dl.dropboxusercontent.com/f.pdf"
    assert json.loads(
        transport.calls[0]["body"])["path"] == "/Invoices/invoice-4137.pdf"


# --- registry wiring ----------------------------------------------------------


def test_new_actions_are_registered():
    assert "zoom_create_meeting" in registry.ACTIONS
    assert "dropbox_get_temp_link" in registry.ACTIONS
    specs = registry.action_specs()
    assert specs["zoom_create_meeting"] == (
        {"connection_id", "topic"},
        {"start_time", "duration", "timezone", "agenda", "settings"})
    assert specs["dropbox_get_temp_link"] == ({"connection_id"}, {"path"})
    duration_field = next(field for field
                          in registry.ACTIONS["zoom_create_meeting"].fields
                          if field["key"] == "duration")
    assert duration_field["type"] == "number"


def test_validate_action_chain_accepts_a_valid_chain():
    registry.validate_action_chain([
        {"type": "zoom_create_meeting", "connection_id": "zoom",
         "topic": "{topic}", "duration": 60},
        {"type": "dropbox_get_temp_link", "connection_id": "dbx",
         "path": "{steps.create.output.path}"},
    ])


def test_validate_action_chain_rejects_missing_topic_and_path():
    with pytest.raises(registry.ActionError, match="missing: topic"):
        registry.validate_action_chain([
            {"type": "zoom_create_meeting", "connection_id": "zoom"}])
    with pytest.raises(registry.ActionError, match="missing: connection_id"):
        registry.validate_action_chain([
            {"type": "dropbox_get_temp_link", "path": "/f.pdf"}])
    # a temp-link chain without a path is legal — it reads the event's file
    registry.validate_action_chain([
        {"type": "dropbox_get_temp_link", "connection_id": "dbx"}])


def test_validate_action_chain_rejects_unknown_keys():
    with pytest.raises(registry.ActionError, match="unknown keys: user_id"):
        registry.validate_action_chain([
            {"type": "zoom_create_meeting", "connection_id": "zoom",
             "topic": "Standup", "user_id": "other-user"}])
    with pytest.raises(registry.ActionError, match="unknown keys: expiry"):
        registry.validate_action_chain([
            {"type": "dropbox_get_temp_link", "connection_id": "dbx",
             "path": "/f.pdf", "expiry": "4h"}])


def test_zoom_duration_field_type_rejects_a_non_numeric_literal():
    with pytest.raises(registry.ActionError, match="duration"):
        registry.validate_field_types(
            {"type": "zoom_create_meeting", "duration": "forty minutes"},
            "zoom_create_meeting")
    # numerics and templates stay legal
    registry.validate_field_types(
        {"type": "zoom_create_meeting", "duration": 45}, "zoom_create_meeting")
    registry.validate_field_types(
        {"type": "zoom_create_meeting", "duration": "{steps.form.output.minutes}"},
        "zoom_create_meeting")
    with pytest.raises(registry.ActionError, match="duration"):
        registry.validate_action_chain([
            {"type": "zoom_create_meeting", "connection_id": "zoom",
             "topic": "Standup", "duration": "soon"}])
