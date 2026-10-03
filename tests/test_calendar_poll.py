"""The google-calendar.events poll source on the trigger machinery:
registration through the builtin-module seam, the scheduled fire's cursor
parking, the max_items budget's interplay with the seen-set, and the public
view.

tests/test_calendar.py pins the fetch's watermark semantics (seed without
emitting, the created filter, oldest-first emission); this file pins how a
stored trigger's fire treats the fetched page — the half that decides
whether a workflow runs twice or an event is lost.
"""
import json
from unittest.mock import patch

import pytest

from plugins.google.connector import calendar
from src.dapier.connections import discovery as provider
from src.dapier.triggers import poll_sources, poll_triggers
from src.dapier.triggers.email_triggers import TriggerError

CAL_ID = "ops@example.test"
EVENT_ID = "1a2B3c4D5e6F7g8H9i0J_k4n9vqe7c9k"

# The seeding watermark pins to the past, so the fixtures' 2026-09-28
# created timestamps are strictly after it whatever the wall clock says.
SEED_NOW = "2026-01-01T00:00:00.000Z"


@pytest.fixture(autouse=True)
def fixed_seed(monkeypatch):
    monkeypatch.setattr(calendar, "_calendar_now", lambda: SEED_NOW)


def google_event(event_id=EVENT_ID, created="2026-09-28T09:14:03.000Z", **extra):
    """One Calendar API event object, the shape events.list returns."""
    return {
        "id": event_id,
        "status": "confirmed",
        "htmlLink": f"https://calendar.google.com/calendar/event?eid={event_id}",
        "created": created,
        "updated": created,
        "summary": "Reviewer call",
        "start": {"dateTime": "2026-09-29T10:00:00+02:00"},
        "end": {"dateTime": "2026-09-29T11:00:00+02:00"},
        **extra,
    }


def events_transport(*events):
    """The listing page, immutable across fires (the provider recycles it)."""
    class Transport:
        def __init__(self):
            self.calls = []

        def __call__(self, method, url, *, headers=None, body=None, timeout=15):
            self.calls.append({"method": method, "url": url})
            return 200, json.dumps({"items": list(events)}).encode()
    return Transport()


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


def stored(body):
    """The stored item build_item would persist for ``body``."""
    return poll_triggers.build_item(body, "op@example.test")


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


def run_fire(item, transport, *, cursors=None):
    """One scheduled fire against a canned page, with real cursor/seen
    machinery and the engine stubbed out."""
    fired_events = []
    cursors = cursors or FakeCursorTable()
    with patch.object(poll_triggers, "get_item", return_value=item), \
         patch.object(provider, "_default_transport", transport), \
         patch.object(poll_triggers, "_bearer_token",
                      lambda connection_id: "fresh-token"), \
         patch("src.dapier.engine.execute",
               side_effect=lambda event, **_kwargs: fired_events.append(event)), \
         patch("src.dapier.engine.notify.notify_failure"):
        result = poll_triggers.fire(item["poll_id"], cursor_table_ref=cursors)
    return result, fired_events, cursors


# --- registration -----------------------------------------------------------------


def test_the_source_resolves_through_the_builtin_module_seam():
    """A scheduled fire resolves the source via poll_sources' builtin module
    list, without the connectors package having been imported first."""
    spec = poll_sources.resolve("google-calendar.events")
    assert (spec.connector, spec.event) == ("google-calendar", "event.new")
    with pytest.raises(ValueError, match="must be one of"):
        poll_sources.resolve("google-calendar.nope")


def test_an_unknown_source_fails_the_save():
    with pytest.raises(TriggerError, match="source must be one of"):
        stored(poll_body(source="google-calendar.nope"))


def test_public_view_names_the_calendar():
    view = poll_triggers.public_view(stored(poll_body()))

    assert view["source"] == "google-calendar.events"
    assert view["calendar_id"] == CAL_ID


# --- scheduled fires ----------------------------------------------------------------


def test_the_seed_fire_emits_nothing_and_parks_the_watermark():
    item = stored(poll_body())
    result, fired, cursors = run_fire(item, events_transport(google_event()))

    assert result["fired"] == 0 and fired == []
    cursor = poll_triggers.get_cursor(item["poll_id"], table=cursors)
    assert cursor  # the parked seeding watermark


def test_an_empty_page_keeps_the_parked_cursor():
    cursors = FakeCursorTable()
    item = stored(poll_body())
    history = google_event(created="2025-06-01T09:14:03.000Z")
    run_fire(item, events_transport(history), cursors=cursors)
    before = poll_triggers.get_cursor(item["poll_id"], table=cursors)

    result, fired, cursors = run_fire(item, events_transport(history),
                                      cursors=cursors)

    assert result["fired"] == 0 and fired == []
    assert poll_triggers.get_cursor(item["poll_id"], table=cursors) == before


def test_the_budget_parks_no_cursor_and_the_seen_set_dedupes_the_refetch():
    """Three fires with max_items 1 and a page of two fresh events: the
    budget fires the first and leaves the cursor (the page was not drained),
    the refetch recognizes the first event in the seen-set and fires only
    the second, and the drained page parks its cursor at last."""
    item = stored(poll_body(max_items=1))
    first = google_event(event_id="e-1", created="2026-09-28T10:00:00.000Z")
    second = google_event(event_id="e-2", created="2026-09-28T11:00:00.000Z")
    transport = events_transport(second, first)  # listed start order
    cursors = FakeCursorTable()

    run_fire(item, events_transport(google_event()), cursors=cursors)  # seed
    result, fired, cursors = run_fire(item, transport, cursors=cursors)
    assert [event["data"]["id"] for event in fired] == ["e-1"]
    # the page still had a fresh item: the fetch's next cursor is not parked
    assert poll_triggers.get_cursor(item["poll_id"], table=cursors) is not None

    result, fired, cursors = run_fire(item, transport, cursors=cursors)
    assert result.get("skipped_seen") == 1
    assert [event["data"]["id"] for event in fired] == ["e-2"]
    assert poll_triggers.get_cursor(item["poll_id"], table=cursors) \
        == "2026-09-28T11:00:00.000Z"

    # the parked cursor now sits at both events: nothing is refetched, and
    # the drain keeps it where it is
    result, fired, cursors = run_fire(item, transport, cursors=cursors)
    assert result["fired"] == 0 and fired == []
    assert poll_triggers.get_cursor(item["poll_id"], table=cursors) \
        == "2026-09-28T11:00:00.000Z"


def test_a_fired_event_publishes_the_calendar_envelope():
    cursors = FakeCursorTable()
    item = stored(poll_body())
    run_fire(item, events_transport(google_event()), cursors=cursors)

    _result, fired, cursors = run_fire(
        item, events_transport(google_event(created="2026-09-28T11:00:00.000Z")),
        cursors=cursors)

    assert len(fired) == 1
    envelope = fired[0]
    assert (envelope["connector"], envelope["event"]) == ("google-calendar", "event.new")
    assert envelope["source"] == "calendar-news"
    assert envelope["data"]["item_id"] == EVENT_ID
    assert envelope["data"]["poll"] == "calendar-news"
