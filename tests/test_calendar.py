"""The Google Calendar connector: event actions, the calendar/event
listings, and the ``google-calendar.events`` poll source.

Zapier's Google Calendar app closed: Create/Quick-add/Find-or-create/
Update/Delete Event actions over a Google connection, a calendars+events
listing for the field pickers, a calendar picker for the trigger config,
the chip's live/history/synthetic sample pull, and a "New Event" poll
source with the drive files source's created-time watermark (first fire
seeds and emits nothing, edits never pose as new events). Transport is
stubbed at the same seams tests/test_drive_changes.py stubs.
"""
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.dapier.connectors import calendar, registry, trigger_discovery  # noqa: F401
from src.dapier.connections import discovery as provider
from src.dapier.connections import tokens
from src.dapier.engine.actions import base
from src.dapier.engine.actions.calendar import run_calendar_create_event
from src.dapier.engine.actions.calendar import run_calendar_delete_event
from src.dapier.engine.actions.calendar import run_calendar_find_events
from src.dapier.engine.actions.calendar import run_calendar_quick_add
from src.dapier.engine.actions.calendar import run_calendar_update_event
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

WEB = Path(__file__).resolve().parents[1] / "src" / "web"
CAL_ID = "ops@example.test"
CAL_Q = urllib.parse.quote(CAL_ID, safe="")
EVENT_ID = "1a2B3c4D5e6F7g8H9i0J_k4n9vqe7c9k"


def google_event(event_id=EVENT_ID, created="2026-09-28T09:14:03.000Z", **extra):
    """One Calendar API event object, the shape events.list returns."""
    return {
        "id": event_id,
        "status": "confirmed",
        "htmlLink": f"https://calendar.google.com/calendar/event?eid={event_id}",
        "created": created,
        "updated": created,
        "summary": "Reviewer call",
        "location": "https://meet.test/reviewer-call",
        "creator": {"email": "ops@example.test", "self": True},
        "start": {"dateTime": "2026-09-29T10:00:00+02:00"},
        "end": {"dateTime": "2026-09-29T11:00:00+02:00"},
        "iCalUID": f"{event_id}@google.com",
        **extra,
    }


class FakeTransport:
    """Canned Calendar API responses, recording every call."""

    def __init__(self, items=None, calendars=None, status=200, created=None):
        self.items = items if items is not None else []
        self.calendars = calendars if calendars is not None else []
        self.status = status
        self.created = created  # None: POSTs echo their body back
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url,
                           "headers": headers, "body": body})
        if method == "GET" and "calendarList" in url:
            return self.status, json.dumps({"items": self.calendars}).encode()
        if method == "GET":
            return self.status, json.dumps({"items": self.items}).encode()
        if method == "DELETE":
            return self.status, b""
        if method == "POST" and body and self.created is None:
            # the API echoes the posted event resource back with its id
            payload = {**json.loads(body), "id": EVENT_ID, "kind": "calendar#event"}
            return self.status, json.dumps(payload).encode()
        return self.status, json.dumps(self.created or google_event()).encode()


def stub_google(monkeypatch):
    """The connection record and its (refreshed) OAuth token, without boto3."""
    monkeypatch.setattr(base, "_connected_connection",
                        lambda connection_id: {
                            "connection_id": connection_id, "provider": "google",
                            "status": "connected"})
    monkeypatch.setattr(tokens, "get_access_token",
                        lambda connection, *, transport=None: ("fresh-token", {}))


# --- engine: create / quick add ------------------------------------------------


def event_action(**overrides):
    action = {"type": "calendar_create_event", "connection_id": "google-calendar",
              "calendar_id": CAL_ID, "summary": "Interview with {name}",
              "start": "2026-10-01T09:00:00", "end": "2026-10-01T10:00:00",
              "timezone": "Europe/Berlin"}
    action.update(overrides)
    return action


EVENT = {"data": {"name": "Ada"}}


def test_create_posts_the_event_and_projects_the_output(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport()
    action = event_action(attendees='["a@example.test", "b@example.test"]',
                          description="Panel", location="Room 4")

    output = run_calendar_create_event(action, EVENT, transport=transport, steps={})

    create = transport.calls[0]
    assert create["method"] == "POST"
    assert f"/calendars/{CAL_Q}/events" in create["url"]
    body = json.loads(create["body"])
    assert body["summary"] == "Interview with Ada"
    assert body["start"] == {"dateTime": "2026-10-01T09:00:00", "timeZone": "Europe/Berlin"}
    assert body["end"] == {"dateTime": "2026-10-01T10:00:00", "timeZone": "Europe/Berlin"}
    assert body["attendees"] == [{"email": "a@example.test"}, {"email": "b@example.test"}]
    assert body["description"] == "Panel" and body["location"] == "Room 4"
    assert output["event_id"] == EVENT_ID
    assert output["start"] == "2026-10-01T09:00:00"
    assert output["all_day"] is False


def test_create_accepts_all_day_dates_and_rejects_mixed_styles(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport()
    output = run_calendar_create_event(
        event_action(start="2026-10-01", end="2026-10-02"), EVENT,
        transport=transport, steps={})

    body = json.loads(transport.calls[0]["body"])
    assert body["start"] == {"date": "2026-10-01"}
    assert output["all_day"] is True

    with pytest.raises(ValueError, match="both"):
        run_calendar_create_event(
            event_action(start="2026-10-01", end="2026-10-02T09:00:00"),
            EVENT, transport=transport, steps={})


def test_create_validates_its_required_fields(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport()
    for key in ("calendar_id", "summary", "start", "end"):
        with pytest.raises(ValueError, match=key):
            run_calendar_create_event(event_action(**{key: ""}), EVENT,
                                      transport=transport, steps={})
    assert transport.calls == []


def test_quick_add_posts_the_one_liner(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport()
    output = run_calendar_quick_add(
        {"type": "calendar_quick_add", "connection_id": "google-calendar",
         "calendar_id": "primary", "text": "Reviewer call tomorrow 10am"},
        EVENT, transport=transport, steps={})

    call = transport.calls[0]
    assert call["method"] == "POST"
    assert "/calendars/primary/events/quickAdd?" in call["url"]
    assert "text=Reviewer+call+tomorrow+10am" in call["url"]
    assert output["summary"] == "Reviewer call"


# --- engine: find (with find-or-create) ----------------------------------------


def find_action(**overrides):
    action = {"type": "calendar_find_events", "connection_id": "google-calendar",
              "calendar_id": CAL_ID}
    action.update(overrides)
    return action


def test_find_queries_the_window_and_reports_the_matches(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport(items=[google_event(), google_event(event_id="second")])
    output = run_calendar_find_events(find_action(query="reviewer"), EVENT,
                                      transport=transport, steps={})

    call = transport.calls[0]
    assert call["method"] == "GET"
    assert f"/calendars/{CAL_Q}/events?" in call["url"]
    assert "singleEvents=true" in call["url"] and "orderBy=startTime" in call["url"]
    assert "q=reviewer" in call["url"]
    assert re.search(r"timeMin=\d{4}-\d{2}-\d{2}T", call["url"])
    assert output["found"] is True and output["created"] is False
    assert output["count"] == 2
    assert output["event"]["event_id"] == EVENT_ID
    assert len(output["events"]) == 2


def test_find_miss_is_not_an_error(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport()
    output = run_calendar_find_events(find_action(query="nothing"), EVENT,
                                      transport=transport, steps={})

    assert output == {"found": False, "created": False, "count": 0,
                      "event": None, "events": []}
    assert [call["method"] for call in transport.calls] == ["GET"]


def test_find_or_create_posts_when_the_search_misses(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport()
    action = find_action(query="reviewer", create_if_missing="true",
                         summary="Review with {name}", start="2026-10-01T09:00:00",
                         end="2026-10-01T10:00:00")
    output = run_calendar_find_events(action, EVENT, transport=transport, steps={})

    assert [call["method"] for call in transport.calls] == ["GET", "POST"]
    body = json.loads(transport.calls[1]["body"])
    assert body["summary"] == "Review with Ada"
    assert output["found"] is False and output["created"] is True
    assert output["event"]["event_id"] == EVENT_ID


def test_find_or_create_without_create_fields_is_a_save_error(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport()
    with pytest.raises(ValueError, match="summary"):
        run_calendar_find_events(find_action(create_if_missing="true"),
                                 EVENT, transport=transport, steps={})
    assert [call["method"] for call in transport.calls] == ["GET"]


# --- engine: update / delete ----------------------------------------------------


def test_update_patches_only_the_fields_that_are_set(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport()
    output = run_calendar_update_event(
        {"type": "calendar_update_event", "connection_id": "google-calendar",
         "calendar_id": CAL_ID, "event_id": EVENT_ID,
         "summary": "Rescheduled review", "start": "2026-10-02T09:00:00"},
        EVENT, transport=transport, steps={})

    call = transport.calls[0]
    assert call["method"] == "PATCH"
    assert f"/calendars/{CAL_Q}/events/{EVENT_ID}" in call["url"]
    body = json.loads(call["body"])
    assert body == {"summary": "Rescheduled review",
                    "start": {"dateTime": "2026-10-02T09:00:00"}}
    assert output["event_id"] == EVENT_ID

    with pytest.raises(ValueError, match="at least one field"):
        run_calendar_update_event(
            {"type": "calendar_update_event", "connection_id": "google-calendar",
             "calendar_id": CAL_ID, "event_id": EVENT_ID},
            EVENT, transport=transport, steps={})


def test_delete_removes_the_event(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport()
    output = run_calendar_delete_event(
        {"type": "calendar_delete_event", "connection_id": "google-calendar",
         "calendar_id": CAL_ID, "event_id": EVENT_ID},
        EVENT, transport=transport, steps={})

    call = transport.calls[0]
    assert call["method"] == "DELETE"
    assert f"/calendars/{CAL_Q}/events/{EVENT_ID}" in call["url"]
    assert output == {"event_id": EVENT_ID, "deleted": True}


def test_provider_errors_raise_readably(monkeypatch):
    stub_google(monkeypatch)
    transport = FakeTransport(status=404)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        run_calendar_quick_add(
            {"type": "calendar_quick_add", "connection_id": "google-calendar",
             "calendar_id": "nope", "text": "x"},
            EVENT, transport=transport, steps={})


# --- discovery: the calendars and events listings -------------------------------


def calendars_transport():
    return FakeTransport(calendars=[
        {"id": CAL_ID, "summary": "Work", "timeZone": "Europe/Berlin",
         "primary": True, "accessRole": "owner"},
        {"id": "holidays", "summary": "Holidays", "accessRole": "reader"},
    ])


def test_discover_lists_the_calendars(monkeypatch):
    transport = calendars_transport()
    monkeypatch.setattr(provider.tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))
    with patch.object(provider, "_default_transport", transport):
        items = provider.discover(
            {"connection_id": "google", "provider": "google"}, "calendars", {})

    assert items[0] == {"id": CAL_ID, "name": "Work", "timeZone": "Europe/Berlin",
                        "primary": True, "accessRole": "owner"}
    assert "calendarList" in transport.calls[0]["url"]


def test_discover_lists_a_calendars_events_and_validates_params(monkeypatch):
    transport = FakeTransport(items=[google_event()])
    connection = {"connection_id": "google", "provider": "google"}
    monkeypatch.setattr(provider.tokens, "get_access_token",
                        lambda connection, transport=None: ("tok", {}))
    with patch.object(provider, "_default_transport", transport):
        items = provider.discover(connection, "google-calendar.events",
                                  {"calendar_id": CAL_ID})

    assert items[0]["id"] == EVENT_ID
    assert items[0]["name"] == "Reviewer call"
    assert "singleEvents=true" in transport.calls[0]["url"]

    with patch.object(provider, "_default_transport", transport):
        with pytest.raises(provider.DiscoveryError, match="calendar_id"):
            provider.discover(connection, "events", {})
        with pytest.raises(provider.DiscoveryError, match="Unknown parameter"):
            provider.discover(connection, "events",
                              {"calendar_id": CAL_ID, "bogus": "1"})


# --- poll source: google-calendar.events ---------------------------------------


def poll_body(**overrides):
    body = {
        "name": "calendar-news",
        "expression": "rate(5 minutes)",
        "source": "google-calendar.events",
        "calendar_id": CAL_ID,
        "connection_id": "google",
        "actions": [{"type": "email_send", "to": "ops@example.test"}],
    }
    body.update(overrides)
    return body


def test_the_poll_source_registers_under_the_calendar_chip():
    source = poll_sources.SOURCES["google-calendar.events"]
    assert (source.connector, source.event) == ("google-calendar", "event.new")
    assert "google-calendar.events" in poll_sources.source_names()
    chip = next(entry for entry in registry.catalog()["connectors"]
                if entry["name"] == "google-calendar")
    assert chip["events"] == ["event.new"]
    assert chip["label"] == "Google Calendar"


def test_poll_save_stores_the_fetch_spec(monkeypatch):
    monkeypatch.setattr(base, "_connected_connection",
                        lambda connection_id: {
                            "connection_id": connection_id, "provider": "google",
                            "status": "connected"})
    item = poll_triggers.build_item(poll_body(), "op@example.test")

    assert item["source"] == "google-calendar.events"
    assert item["calendar_id"] == CAL_ID
    assert item["connection_id"] == "google"
    assert item["cursor_mode"] == "next_cursor"
    assert item["id_path"] == "id"
    assert item["url"] == ""

    with pytest.raises(TriggerError, match="calendar_id"):
        poll_triggers.build_item(poll_body(calendar_id=""), "op@example.test")
    with pytest.raises(TriggerError, match="connection_id"):
        poll_triggers.build_item(poll_body(connection_id=""), "op@example.test")


def events_transport(*events):
    class Transport:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, *, headers=None, body=None, timeout=15):
            self.calls.append({"method": method, "url": url})
            return 200, json.dumps({"items": list(events)}).encode()
    return Transport()


def test_first_fire_seeds_the_watermark_and_emits_nothing(monkeypatch):
    monkeypatch.setattr(poll_triggers, "_bearer_token",
                        lambda connection_id: "fresh-token")
    transport = events_transport(google_event())
    items, cursor = calendar._events_poll_fetch(
        {"connection_id": "google", "calendar_id": CAL_ID},
        None, transport=transport)

    assert items == [] and cursor
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", cursor)
    assert "timeMin=" in transport.calls[0]["url"]
    assert f"/calendars/{CAL_Q}/events?" in transport.calls[0]["url"]


def test_fires_only_events_created_after_the_watermark_oldest_first(monkeypatch):
    monkeypatch.setattr(poll_triggers, "_bearer_token",
                        lambda connection_id: "fresh-token")
    old = google_event(event_id="old", created="2026-09-28T08:00:00.000Z")
    newer = google_event(event_id="newer", created="2026-09-28T09:14:03.000Z")
    newest = google_event(event_id="newest", created="2026-09-28T10:00:00.000Z")
    transport = events_transport(newest, old, newer)  # listed start order, not created

    items, cursor = calendar._events_poll_fetch(
        {"connection_id": "google", "calendar_id": CAL_ID},
        "2026-09-28T08:30:00.000Z", transport=transport)

    assert [item["id"] for item in items] == ["newer", "newest"]
    assert cursor == "2026-09-28T10:00:00.000Z"

    items, cursor = calendar._events_poll_fetch(
        {"connection_id": "google", "calendar_id": CAL_ID},
        cursor, transport=transport)
    assert items == [] and cursor == "2026-09-28T10:00:00.000Z"


def test_a_missing_fetch_spec_fails_the_fire_readably():
    with pytest.raises(RuntimeError, match="calendar_id"):
        calendar._events_poll_fetch({"connection_id": "google"}, "x", transport=events_transport())
    with pytest.raises(RuntimeError, match="connection_id"):
        calendar._events_poll_fetch({"calendar_id": CAL_ID}, "x", transport=events_transport())


class FakeCursorTable:
    """DynamoDB stand-in for poll cursors and seen-sets."""

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


def test_a_scheduled_fire_publishes_the_new_event(monkeypatch):
    """End to end on the fire path: seed fire parks the watermark, the next
    fire emits the fresh event through the engine."""
    fired = []
    cursors = FakeCursorTable()
    stored = poll_triggers.build_item(poll_body(), "op@example.test")
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: stored)
    monkeypatch.setattr(poll_triggers, "_bearer_token", lambda connection_id: "fresh-token")
    monkeypatch.setattr("src.dapier.engine.execute",
                        lambda event, **_kwargs: fired.append(event))
    monkeypatch.setattr("src.dapier.engine.notify.notify_failure", lambda *a, **k: None)

    with patch.object(provider, "_default_transport", events_transport(google_event())):
        result = poll_triggers.fire(stored["poll_id"], cursor_table_ref=cursors)
    assert result.get("emitted") in (0, None)
    assert fired == []

    # The seed fire parks the watermark at now, so the "new" event must be
    # created after that instant — a fixed clock time goes stale daily.
    future = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    with patch.object(provider, "_default_transport",
                      events_transport(google_event(created=future))):
        poll_triggers.fire(stored["poll_id"], cursor_table_ref=cursors)

    assert len(fired) == 1
    assert fired[0]["connector"] == "google-calendar"
    assert fired[0]["event"] == "event.new"
    assert fired[0]["data"]["id"] == EVENT_ID


# --- the chip's sample pull ------------------------------------------------------


def test_sample_pull_falls_back_to_the_documented_example(monkeypatch):
    monkeypatch.setattr(calendar.trigger_discovery, "history_sample",
                        lambda connector, event=None: None)
    result = calendar._fetch_calendar_sample()
    assert result["source"] == "synthetic"
    assert result["sample"]["connector"] == "google-calendar"
    assert result["sample"]["event"] == "event.new"
    assert result["sample"]["data"]["summary"] == "Reviewer call"


def test_sample_pull_runs_the_stored_poll_live(monkeypatch):
    stored = {"poll_id": "calendar-news", "source": "google-calendar.events",
              "calendar_id": CAL_ID, "connection_id": "google",
              "cursor_mode": "next_cursor", "id_path": "id"}
    monkeypatch.setattr(poll_triggers, "get_item",
                        lambda name, table_ref=None: stored)
    monkeypatch.setattr(poll_triggers, "_bearer_token", lambda connection_id: "fresh-token")
    monkeypatch.setattr(provider, "_default_transport", events_transport(google_event()))

    result = calendar._fetch_calendar_sample("calendar-news")
    assert result["source"] == "live"
    assert result["sample"]["event"] == "event.new"
    assert result["sample"]["data"]["summary"] == "Reviewer call"


# --- registry + console wiring ---------------------------------------------------


def test_the_registry_serves_the_actions_and_the_provider_mapping():
    specs = registry.action_specs()
    assert specs["calendar_create_event"][0] == frozenset(
        {"connection_id", "calendar_id", "summary", "start", "end"})
    create = registry.ACTIONS["calendar_create_event"]
    calendar_field = next(field for field in create.fields if field["key"] == "calendar_id")
    assert calendar_field["discover"] == {"resource": "google-calendar.calendars"}
    find = registry.ACTIONS["calendar_find_events"]
    assert find.optional >= {"create_if_missing", "query", "time_min", "time_max"}
    sources = {f"{d.connector}.{d.name}" for d in registry.discoveries_for_provider("google")}
    assert {"google-calendar.calendars", "google-calendar.events"} <= sources


def test_the_console_dialog_carries_the_new_source():
    html = (WEB / "index.html").read_text()
    select = re.search(r'name="source">(.*?)</select>', html, re.S).group(1)
    assert 'value="google-calendar.events"' in select

    js = (WEB / "js" / "views" / "triggers.js").read_text()
    assert "'google-calendar.events'" in js  # connection-required list
    assert "'calendar_id'" in js  # an Options key, shown as the Watches target

    connections = (WEB / "js" / "views" / "connections.js").read_text()
    assert "auth/calendar.readonly" in connections  # listings need read scope


def test_the_trigger_config_serves_calendar_options(monkeypatch):
    cat = trigger_discovery.trigger_discovery_catalog()
    assert "google-calendar" in cat["sample"] and "google-calendar" in cat["options"]
    entry = next(item for item in trigger_discovery.TRIGGER_DISCOVERIES.values()
                 if item.connector == "google-calendar" and item.kind == "options")
    assert entry.resource == "google-calendar.calendars"
