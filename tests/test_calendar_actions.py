"""The Google Calendar actions through the registry: save-time validation,
the run_action dispatch, the registered discovery entries, and the scope
handling of a calendar-scoped Google connection.

tests/test_calendar.py drives the engine runners and the fetch seams
directly; this file pins the connector-registry half — what a stored
trigger's action chain answers to at save time, what ``run_action`` (the
engine's dispatch) actually calls, and the listings the
/api/*/connections/.../discover endpoints serve through the registry.
"""
import json
from unittest.mock import patch

import pytest

from src.dapier.connections.providers import oauth_providers
from src.dapier.engine.actions import base
from src.dapier.connectors import calendar, registry  # noqa: F401  (calendar: registration)
from src.dapier.connections import discovery as provider
from src.dapier.connections import tokens

CAL_ID = "ops@example.test"
EVENT_ID = "1a2B3c4D5e6F7g8H9i0J_k4n9vqe7c9k"

EVENT = {"data": {"name": "Ada"}}

CAL_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events.owned"
CAL_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
USERINFO_SCOPE = "https://www.googleapis.com/auth/userinfo.email"


def google_event(event_id=EVENT_ID, **extra):
    """One Calendar API event object, the shape events.list returns."""
    return {
        "id": event_id,
        "status": "confirmed",
        "htmlLink": f"https://calendar.google.com/calendar/event?eid={event_id}",
        "created": "2026-09-28T09:14:03.000Z",
        "updated": "2026-09-28T09:14:03.000Z",
        "summary": "Reviewer call",
        "start": {"dateTime": "2026-09-29T10:00:00+02:00"},
        "end": {"dateTime": "2026-09-29T11:00:00+02:00"},
        **extra,
    }


class FakeTransport:
    """Canned Calendar API responses, recording every call."""

    def __init__(self, items=None, calendars=None, status=200):
        self.items = items if items is not None else []
        self.calendars = calendars if calendars is not None else []
        self.status = status
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url, "body": body})
        if "calendarList" in url:
            return self.status, json.dumps({"items": self.calendars}).encode()
        if method == "POST":
            return self.status, json.dumps(google_event()).encode()
        return self.status, json.dumps({"items": self.items}).encode()


@pytest.fixture
def google_seams(monkeypatch):
    """The connection record and its (refreshed) OAuth token, without boto3."""
    monkeypatch.setattr(base, "_connected_connection",
                        lambda connection_id: {
                            "connection_id": connection_id, "provider": "google",
                            "status": "connected"})
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, *, transport=None: ("fresh-token", {}))


# --- save-time validation (validate_action_chain) ---------------------------------


def create_step(**overrides):
    step = {"type": "calendar_create_event", "connection_id": "google-calendar",
            "calendar_id": CAL_ID, "summary": "Interview with {name}",
            "start": "2026-10-01T09:00:00", "end": "2026-10-01T10:00:00"}
    step.update(overrides)
    return step


def test_a_calendar_chain_saves():
    chain = [
        create_step(),
        {"type": "calendar_update_event", "connection_id": "google-calendar",
         "calendar_id": CAL_ID, "event_id": "{steps.create.output.event_id}",
         "summary": "Interview with {name} (moved)"},
    ]
    assert registry.validate_action_chain(chain) == chain


def test_a_missing_required_field_fails_the_save():
    with pytest.raises(registry.ActionError, match="calendar_create_event"):
        registry.validate_action_chain([create_step(end="")])


def test_an_unknown_field_fails_the_save():
    with pytest.raises(registry.ActionError, match="unknown keys: bogus"):
        registry.validate_action_chain([create_step(bogus="1")])


def test_a_bad_template_in_a_calendar_field_fails_the_save():
    step = create_step(summary="Interview with {name|upper_case}")
    with pytest.raises(registry.ActionError, match="unknown formatter"):
        registry.validate_action_chain([step])


# --- the engine's dispatch through the registry -----------------------------------


def test_run_action_dispatches_the_registered_runners(google_seams):
    """The registry lambdas swallow the engine's workflow_id and land on the
    engine runner; the default transport is the seam, since scheduled runs
    carry no transport."""
    transport = FakeTransport()
    with patch.object(base, "_default_transport", transport):
        output = registry.run_action(
            {"type": "calendar_quick_add", "connection_id": "google-calendar",
             "calendar_id": "primary", "text": "Reviewer call tomorrow 10am"},
            EVENT, "wf-1")

    post = transport.calls[0]
    assert post["method"] == "POST"
    assert "/calendars/primary/events/quickAdd" in post["url"]
    assert output["event_id"] == EVENT_ID


def test_run_action_names_an_unknown_type():
    with pytest.raises(ValueError, match="unsupported action"):
        registry.run_action({"type": "calendar_nope"}, EVENT)


# --- the registered discovery entries ----------------------------------------------


def test_the_calendars_listing_runs_through_the_registry_entry(monkeypatch):
    monkeypatch.setattr(provider, "access_token",
                        lambda connection, *, transport=None: "fresh-token")
    transport = FakeTransport(calendars=[
        {"id": CAL_ID, "summary": "Work", "timeZone": "Europe/Berlin",
         "primary": True, "accessRole": "owner"}])
    entry = registry.DISCOVERIES["google-calendar.calendars"]

    items = entry.run({"connection_id": "google", "provider": "google"},
                      {}, transport=transport)

    assert items[0]["id"] == CAL_ID and items[0]["name"] == "Work"
    assert "calendarList" in transport.calls[0]["url"]


def test_the_events_listing_needs_its_required_param(monkeypatch):
    monkeypatch.setattr(provider, "access_token",
                        lambda connection, *, transport=None: "fresh-token")
    transport = FakeTransport(items=[google_event()])
    entry = registry.DISCOVERIES["google-calendar.events"]
    connection = {"connection_id": "google", "provider": "google"}

    with pytest.raises(provider.DiscoveryError, match="calendar_id"):
        entry.run(connection, {}, transport=transport)

    items = entry.run(connection, {"calendar_id": CAL_ID}, transport=transport)
    assert items[0]["id"] == EVENT_ID
    assert "timeMin=" in transport.calls[-1]["url"]


# --- scope handling ----------------------------------------------------------------


def test_calendar_scopes_normalize_deduplicated():
    scopes = oauth_providers.normalize_scopes(
        "google", [CAL_EVENTS_SCOPE, CAL_READONLY_SCOPE, USERINFO_SCOPE,
                   CAL_READONLY_SCOPE])
    assert scopes == sorted({CAL_EVENTS_SCOPE, CAL_READONLY_SCOPE, USERINFO_SCOPE})


def test_a_calendar_scoped_connection_verifies():
    """Verification is the userinfo.email identity check — the calendar
    scopes need no adapter changes to pass (docs/connectors/calendar.md)."""
    def transport(method, url, *, headers=None, body=None, timeout=15):
        assert "oauth2/v3/userinfo" in url
        assert headers["authorization"] == "Bearer fresh-token"
        return 200, json.dumps({
            "email": "ops@example.test", "email_verified": True,
            "name": "Ops",
        }).encode()

    account_id, title = oauth_providers.verify_account(
        "google", "fresh-token", transport=transport)
    assert (account_id, title) == ("ops@example.test", "Ops")
