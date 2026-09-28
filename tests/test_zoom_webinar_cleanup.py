"""zoom_delete_webinar / zoom_list_past_webinar_participants: the webinar
cleanup pair.

The zoom_delete_meeting / zoom_list_past_participants shapes one level up:
DELETE /webinars/{id} (occurrence-scopable for recurring series) and GET
/past_webinars/{id}/participants (paginated, attendance rows for emails or
sheets). Runners are driven with a fake transport and the connection/token
seams patched — the test_round_zoom pattern.
"""
import json
import urllib.parse
from unittest.mock import patch

import pytest

from src.dapier.connectors import registry
from src.dapier.engine.actions.zoom import (
    run_zoom_delete_webinar,
    run_zoom_list_past_webinar_participants,
)

import src.dapier.connectors  # noqa: F401  (import = registration)

ZOOM_CONNECTION = {"connection_id": "zoom-main", "provider": "zoom",
                   "status": "connected", "credential_id": "oauth#zoom-main"}

EVENT = {"id": "evt/1", "connector": "schedule", "event": "schedule.fired",
         "occurred_at": "2026-09-28T09:00:00+00:00",
         "data": {"webinar_id": "98765432100"}}

PARTICIPANTS_PAGE = {"participants": [
    {"name": "Ada Byron", "user_email": "ada@example.com",
     "join_time": "2026-09-27T15:02:00Z", "leave_time": "2026-09-27T16:00:00Z",
     "duration": 58},
]}


def fake_transport(routes):
    """Route by URL substring to (status, body bytes); record every call."""
    calls = []

    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url, "headers": headers})
        for substring, status, payload in routes:
            if substring in url:
                return status, json.dumps(payload).encode() if payload else b""
        raise AssertionError(f"unexpected provider call: {method} {url}")

    transport.calls = calls
    return transport


def run_zoom_action(runner, action, event=None, transport=None):
    action = {"type": runner.__name__.replace("run_", "", 1),
              "connection_id": "zoom-main", **action}
    with patch("src.dapier.engine.actions.zoom._zoom_connection",
               return_value=dict(ZOOM_CONNECTION)), \
         patch("src.dapier.connections.tokens.get_access_token",
               return_value=("tok", {})):
        return runner(action, event or EVENT, transport=transport)


def _query(call):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(call["url"]).query)


# --- zoom_delete_webinar -------------------------------------------------------


def test_delete_webinar_removes_one_webinar():
    transport = fake_transport([("/webinars/", 204, None)])

    output = run_zoom_action(run_zoom_delete_webinar,
                             {"webinar_id": "98765432100"},
                             transport=transport)

    assert output == {"deleted": True, "webinar_id": "98765432100"}
    call, = transport.calls
    assert call["method"] == "DELETE"
    assert call["url"] == "https://api.zoom.us/v2/webinars/98765432100"
    assert call["headers"]["authorization"] == "Bearer tok"


def test_delete_webinar_scopes_to_one_occurrence_of_a_series():
    transport = fake_transport([("/webinars/", 204, None)])

    output = run_zoom_action(
        run_zoom_delete_webinar,
        {"webinar_id": "98765432100", "occurrence_id": "occ-9"},
        transport=transport)

    assert output == {"deleted": True, "webinar_id": "98765432100",
                      "occurrence_id": "occ-9"}
    assert _query(transport.calls[0])["occurrence_id"] == ["occ-9"]


def test_delete_webinar_requires_webinar_id():
    transport = fake_transport([])
    with pytest.raises(ValueError, match="requires webinar_id"):
        run_zoom_action(run_zoom_delete_webinar, {}, transport=transport)
    assert transport.calls == []


def test_delete_webinar_surfaces_provider_refusals():
    transport = fake_transport([
        ("/webinars/", 404, {"message": "webinar not found"})])
    with pytest.raises(RuntimeError, match="HTTP 404.*webinar not found"):
        run_zoom_action(run_zoom_delete_webinar,
                        {"webinar_id": "98765432100"}, transport=transport)


# --- zoom_list_past_webinar_participants ---------------------------------------


def test_past_webinar_participants_lists_attendees():
    transport = fake_transport([("/past_webinars/", 200, PARTICIPANTS_PAGE)])

    output = run_zoom_action(run_zoom_list_past_webinar_participants,
                             {"webinar_id": "98765432100"},
                             transport=transport)

    assert output == {"participants": [
        {"name": "Ada Byron", "user_email": "ada@example.com",
         "join_time": "2026-09-27T15:02:00Z",
         "leave_time": "2026-09-27T16:00:00Z", "duration": 58}],
        "count": 1, "webinar_id": "98765432100"}
    call, = transport.calls
    assert call["method"] == "GET"
    assert call["url"].startswith(
        "https://api.zoom.us/v2/past_webinars/98765432100/participants")
    assert _query(call)["per_page"] == ["300"]


def test_past_webinar_participants_follow_zoom_pagination():
    transport = fake_transport([
        ("next_page_token=NEXT", 200, {"participants": [
            {"name": "Grace Hopper", "user_email": "grace@example.com"}]}),
        ("/past_webinars/", 200,
         {**PARTICIPANTS_PAGE, "next_page_token": "NEXT"}),
    ])

    output = run_zoom_action(run_zoom_list_past_webinar_participants,
                             {"webinar_id": "98765432100"},
                             transport=transport)

    assert output["count"] == 2
    assert output["participants"][1]["name"] == "Grace Hopper"
    assert _query(transport.calls[1])["next_page_token"] == ["NEXT"]


def test_past_webinar_participants_surfaces_provider_refusals():
    transport = fake_transport([
        ("/past_webinars/", 404, {"message": "webinar not found"})])
    with pytest.raises(RuntimeError, match="HTTP 404"):
        run_zoom_action(run_zoom_list_past_webinar_participants,
                        {"webinar_id": "98765432100"}, transport=transport)


# --- registry parity -------------------------------------------------------


def test_cleanup_pair_is_registered_with_webinar_pickers():
    for action_type in ("zoom_delete_webinar",
                        "zoom_list_past_webinar_participants"):
        entry = registry.ACTIONS[action_type]
        assert entry.required == frozenset({"connection_id", "webinar_id"})
        hints = [field.get("discover", {}).get("resource")
                 for field in entry.fields if field.get("discover")]
        assert hints == ["zoom.webinars"], action_type
        assert registry.DISCOVERIES["zoom.webinars"].run is not None
