"""Google Calendar connector: create/quick-add/find/update/delete event
actions over a Google connection, plus the calendar and event listings, the
trigger config's calendar picker, and the ``google-calendar.events`` poll
source (new event on the poll schedule).

The Discovery/ConnectionTest callables delegate to the shared provider
layer (``connections.discovery``) like every Google connector here, so the
catalog metadata and the live listings can never drift apart. The poll
source keys on each event's ``created`` timestamp — the drive files
source's watermark applied to events: the first fire seeds the watermark
and emits nothing, later fires emit events created strictly after it,
oldest first, so enabling a trigger doesn't fire the calendar's whole
history and a later edit never poses as a new event.
"""
import urllib.parse
from datetime import datetime, timedelta, timezone

from src.dapier.connections import discovery as provider
from plugins.google.runners.calendar import (
    run_calendar_create_event,
    run_calendar_delete_event,
    run_calendar_find_events,
    run_calendar_quick_add,
    run_calendar_update_event,
)
from src.dapier.triggers.poll_sources import PollSource, register_source
from src.dapier.connectors.registry import Action, Discovery, register, register_discovery
from src.dapier.connectors import trigger_discovery
from src.dapier.connectors.trigger_discovery import (
    DEFAULT_LIMIT,
    TriggerDiscovery,
    options_from_registry,
    register_trigger_discovery,
)


def _run_calendars(connection, params, *, transport=None):
    return provider.discover(connection, "calendars", params, transport=transport)


def _run_events(connection, params, *, transport=None):
    return provider.discover(connection, "events", params, transport=transport)


register_discovery(Discovery(
    name="calendars",
    connector="google-calendar",
    label="Calendars",
    description="Calendars the connection can see, including the primary",
    run=_run_calendars,
))

register_discovery(Discovery(
    name="events",
    connector="google-calendar",
    label="Calendar events",
    description="Upcoming events in one calendar, start order",
    params=({"key": "calendar_id", "label": "Calendar ID", "type": "text", "required": True,
             "help": "Calendar ID from the calendars list"},
            {"key": "query", "label": "Query", "type": "text",
             "help": "Text to match against event fields"}),
    run=_run_events,
))

_CALENDAR_ID_FIELD = {
    "key": "calendar_id", "label": "Calendar ID", "placeholder": "primary",
    "required": True,
    "discover": {"resource": "google-calendar.calendars"},
    "help": "Browse the connection's calendars to pick one",
}

register(Action(
    type="calendar_create_event",
    label="Google Calendar",
    icon="calendar",
    description="Create an event in a calendar (Create Detailed Event)",
    run=lambda action, event, workflow_id, steps=None: run_calendar_create_event(
        action, event, steps=steps),
    required=frozenset({"connection_id", "calendar_id", "summary", "start", "end"}),
    optional=frozenset({"description", "location", "attendees", "timezone"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google-calendar",
         "required": True},
        dict(_CALENDAR_ID_FIELD),
        {"key": "summary", "label": "Title", "placeholder": "Interview with {name}",
         "required": True},
        {"key": "start", "label": "Starts", "placeholder": "2026-10-01T09:00:00 or 2026-10-01",
         "required": True,
         "help": "ISO datetime, or a bare YYYY-MM-DD for an all-day event"},
        {"key": "end", "label": "Ends", "placeholder": "2026-10-01T10:00:00 or 2026-10-01",
         "required": True,
         "help": "Same shape as Starts — dates with dates, datetimes with datetimes"},
        {"key": "timezone", "label": "Time zone", "placeholder": "Europe/Berlin",
         "help": "IANA name for the datetimes; omitted means floating time"},
        {"key": "description", "label": "Description", "type": "textarea",
         "placeholder": "Notes, links, agendas"},
        {"key": "location", "label": "Location", "placeholder": "Room 4 / https://meet.test/x"},
        {"key": "attendees", "label": "Attendees (JSON)", "type": "textarea",
         "placeholder": '["a@example.test", "b@example.test"]',
         "help": "JSON array of emails (or {email: …} objects), or a comma-separated list"},
    ),
))

register(Action(
    type="calendar_quick_add",
    label="Google Calendar (quick add)",
    icon="calendar",
    description="Create an event from one line of text (Quick Add Event)",
    run=lambda action, event, workflow_id, steps=None: run_calendar_quick_add(
        action, event, steps=steps),
    required=frozenset({"connection_id", "calendar_id", "text"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google-calendar",
         "required": True},
        dict(_CALENDAR_ID_FIELD),
        {"key": "text", "label": "Event text", "required": True,
         "placeholder": "Reviewer call tomorrow 10am"},
    ),
))

register(Action(
    type="calendar_find_events",
    label="Google Calendar (find event)",
    icon="calendar",
    description="Find events matching a text query, optionally create the "
                "first one when nothing matches (Find or Create Event). "
                "Output: {found, created, count, event, events}; with "
                "create_if_missing the miss posts the event and "
                "created: True.",
    run=lambda action, event, workflow_id, steps=None: run_calendar_find_events(
        action, event, steps=steps),
    required=frozenset({"connection_id", "calendar_id"}),
    optional=frozenset({"query", "time_min", "time_max",
                        "create_if_missing", "summary", "start", "end",
                        "description", "location", "attendees", "timezone"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google-calendar",
         "required": True},
        dict(_CALENDAR_ID_FIELD),
        {"key": "query", "label": "Search text", "placeholder": "{name}",
         "help": "Free-text match across event fields; empty lists the window"},
        {"key": "time_min", "label": "From", "placeholder": "2026-09-01T00:00:00Z",
         "help": "Default: yesterday"},
        {"key": "time_max", "label": "Until", "placeholder": "2026-12-31T23:59:59Z",
         "help": "Default: the end of the next quarter"},
        {"key": "create_if_missing", "label": "Create if missing", "type": "boolean",
         "default": "false",
         "help": "Post the event from the fields below when nothing matches"},
        {"key": "summary", "label": "Create title", "placeholder": "Sync with {name}",
         "help": "Only used when Create if missing is on"},
        {"key": "start", "label": "Create starts", "placeholder": "2026-10-01T09:00:00",
         "help": "Only used when Create if missing is on"},
        {"key": "end", "label": "Create ends", "placeholder": "2026-10-01T10:00:00",
         "help": "Only used when Create if missing is on"},
        {"key": "timezone", "label": "Time zone", "placeholder": "Europe/Berlin"},
        {"key": "description", "label": "Create description", "type": "textarea"},
        {"key": "location", "label": "Create location"},
        {"key": "attendees", "label": "Create attendees (JSON)", "type": "textarea",
         "placeholder": '["a@example.test"]'},
    ),
))

register(Action(
    type="calendar_update_event",
    label="Google Calendar (update event)",
    icon="calendar",
    description="Patch the provided fields of one event (Update Event)",
    run=lambda action, event, workflow_id, steps=None: run_calendar_update_event(
        action, event, steps=steps),
    required=frozenset({"connection_id", "calendar_id", "event_id"}),
    optional=frozenset({"summary", "description", "location", "start", "end",
                        "attendees", "timezone"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google-calendar",
         "required": True},
        dict(_CALENDAR_ID_FIELD),
        {"key": "event_id", "label": "Event ID", "placeholder": "{steps.find.event.event_id}",
         "required": True,
         "help": "From find's output, the trigger payload's id, or the event's link tail"},
        {"key": "summary", "label": "Title"},
        {"key": "start", "label": "Starts", "placeholder": "2026-10-02T09:00:00"},
        {"key": "end", "label": "Ends", "placeholder": "2026-10-02T10:00:00"},
        {"key": "timezone", "label": "Time zone", "placeholder": "Europe/Berlin"},
        {"key": "description", "label": "Description", "type": "textarea"},
        {"key": "location", "label": "Location"},
        {"key": "attendees", "label": "Attendees (JSON)", "type": "textarea",
         "help": "Replaces the attendee list; an empty list clears it only "
                 "when sent as []"},
    ),
))

register(Action(
    type="calendar_delete_event",
    label="Google Calendar (delete event)",
    icon="calendar",
    description="Delete one event from a calendar (Delete Event)",
    run=lambda action, event, workflow_id, steps=None: run_calendar_delete_event(
        action, event, steps=steps),
    required=frozenset({"connection_id", "calendar_id", "event_id"}),
    fields=(
        {"key": "connection_id", "label": "Connection ID", "placeholder": "google-calendar",
         "required": True},
        dict(_CALENDAR_ID_FIELD),
        {"key": "event_id", "label": "Event ID", "placeholder": "{steps.find.event.event_id}",
         "required": True},
    ),
))


# --- trigger discovery: calendar options for the trigger config -------------

def _fetch_calendar_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Calendar options via the registry listing (first connected Google
    connection when no id is named) — the trigger config's calendar picker."""
    return options_from_registry(
        "google-calendar.calendars", connection_id, limit, provider="google",
        option_of=lambda item: {"value": item.get("id"),
                                "label": item.get("name") or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="google-calendar", label="Google Calendar", kind="options",
    resource="google-calendar.calendars",
    fetch=_fetch_calendar_options))


def _fetch_event_options(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """Event options for one calendar via the registry listing. The event
    slot carries the ``<calendar_id>`` (trigger_discovery.listing_params);
    the value is the event id, labeled with its summary and start."""
    return options_from_registry(
        "google-calendar.events", connection_id, limit, provider="google",
        params=trigger_discovery.listing_params(event, ("calendar_id",)),
        option_of=lambda item: {"value": item.get("id"),
                                "label": " · ".join(str(part) for part in
                                                    (item.get("name"), item.get("start")) if part)
                                or item.get("id")})


register_trigger_discovery(TriggerDiscovery(
    connector="google-calendar", label="Google Calendar", kind="options",
    resource="google-calendar.events",
    fetch=_fetch_event_options))


# --- poll source: "New Event" on the poll-trigger schedule ------------------
#
# events.list is JSON, so the generic poll trigger could read it — but a
# listing has no "new" semantics: pickers order by start time, so adding a
# far-future event or an edit to an old one re-lists it at the top. The
# ``google-calendar.events`` source owns the transform — a created-time
# watermark seeded on the first fire, per-event ids the seen store
# dedupes — and publishes ``google-calendar``/``event.new`` so workflows
# match the palette chip while staying scoped through the poll-name filter.

CALENDAR_POLL_PAGE_SIZE = 250
CALENDAR_POLL_PAGES = 2  # ~500 events per fire

# Older than any Calendar created timestamp: the discovery sample's "show
# the calendar's next event" fetch runs against this watermark.
CALENDAR_EPOCH_CURSOR = "0000-01-01T00:00:00.000Z"


def _calendar_now():
    """Calendar-format UTC now (RFC 3339 with a second's precision); the
    created timestamps it watermarks against share the shape, so plain text
    comparison orders them."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _events_poll_validate(body):
    """Save-time fetch spec: ``calendar_id`` (required — Zapier's trigger is
    calendar-scoped), the ``connection_id`` of the Google connection to
    poll as (required — the fetch refreshes its OAuth token), plus the
    fetch defaults every stored calendar poll carries."""
    from src.dapier.triggers.email_triggers import TriggerError

    body = body if isinstance(body, dict) else {}
    calendar_id = str(body.get("calendar_id") or "").strip()
    if not calendar_id:
        raise TriggerError("calendar_id is required: name the calendar to watch")
    if not str(body.get("connection_id") or "").strip():
        raise TriggerError(
            "connection_id is required: pick the Google connection to poll as")
    return {
        "calendar_id": calendar_id,
        "cursor_mode": "next_cursor",
        "id_path": "id",
        "url": "",
    }


def _list_calendar_events(token, calendar_id, *, transport=None):
    """The calendar's upcoming events (up to ~500, two pages of 250),
    expanded per occurrence in start order, with the fields the events and
    pickers share. The window opens an hour back so an event that just
    started is still listed."""
    page_params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": CALENDAR_POLL_PAGE_SIZE,
        "timeMin": (datetime.now(timezone.utc) - timedelta(hours=1))
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    quoted = urllib.parse.quote(str(calendar_id), safe="")
    events = []
    for _page in range(CALENDAR_POLL_PAGES):
        url = (f"{provider.CALENDAR_API_URL}/calendars/{quoted}/events?"
               + urllib.parse.urlencode(page_params))
        data = provider._request("GET", url, token, None, transport=transport)
        page = [entry for entry in data.get("items") or []
                if isinstance(entry, dict) and entry.get("id")]
        events.extend(page)
        if not page or len(events) >= CALENDAR_POLL_PAGE_SIZE * CALENDAR_POLL_PAGES \
                or not data.get("nextPageToken"):
            return events
        page_params = {**page_params, "pageToken": data["nextPageToken"]}
    return events


def _events_poll_fetch(item, cursor=None, *, transport=None):
    """One poll page as ``(items, next_cursor)`` for ``triggers.poll_sources``.

    Lists the calendar through Calendar's events.list (the provider's shared
    request path), with the bearer token from ``poll_triggers._bearer_token``
    so the connection's OAuth token is refreshed exactly like the classic
    fetch. Items are the API's own event objects (id, summary, start, end,
    created, updated, htmlLink, …) — JSON-safe as returned.

    The cursor is an ISO ``created`` watermark (the API renders uniform UTC
    timestamps, so text comparison orders them). With no stored cursor —
    the first fire after enabling — the fetch only seeds the watermark at
    now and emits nothing: the calendar's existing events are history, not
    news. With a cursor, only events created strictly after it fire, oldest
    first, and the parked cursor is the newest fired created (the incoming
    one when nothing qualifies; ``fire`` parks it only once the page
    drains). Keying on created means an edit to an old event never fires a
    bogus "event.new"; the seen store then dedupes the re-listed id. A
    recurring series' occurrences carry the series' creation time, so its
    later occurrences don't fire one by one. Raises ``RuntimeError`` on a
    failed fetch, like every poll source.
    """
    from src.dapier.triggers import poll_triggers

    if not str(item.get("connection_id") or "").strip():
        raise RuntimeError("poll source 'google-calendar.events' needs connection_id: "
                           "the Google connection to poll as")
    calendar_id = str(item.get("calendar_id") or "").strip()
    if not calendar_id:
        raise RuntimeError("poll source 'google-calendar.events' needs a stored calendar_id")
    token = poll_triggers._bearer_token(item["connection_id"])
    try:
        events = _list_calendar_events(token, calendar_id, transport=transport)
    except provider.DiscoveryError as exc:
        raise RuntimeError(f"calendar poll failed: {exc}") from None
    if cursor is None:
        # First fire: seed the watermark at now (the calendar's existing
        # events predate it) without emitting anything.
        return [], _calendar_now()
    watermark = str(cursor)
    fresh = sorted(
        (event for event in events
         if str(event.get("created") or "") > watermark),
        key=lambda event: (str(event.get("created") or ""), str(event.get("id") or "")))
    return fresh, str(fresh[-1]["created"]) if fresh else watermark


def _events_poll_view(item):
    """The provider params ``public_view`` shows beside ``source``."""
    return {"calendar_id": item.get("calendar_id")}


register_source(PollSource(
    name="google-calendar.events", connector="google-calendar", event="event.new",
    label="Google Calendar", validate=_events_poll_validate,
    fetch=_events_poll_fetch, view=_events_poll_view))


# --- trigger discovery: the chip's sample pull -------------------------------

_CALENDAR_SYNTHETIC_EVENT = {
    "id": "1a2B3c4D5e6F7g8H9i0J_k4n9vqe7c9k",
    "status": "confirmed",
    "htmlLink": "https://calendar.google.com/calendar/event?eid=bmV3LXJldmlld2Vy",
    "created": "2026-09-28T09:14:03.000Z",
    "updated": "2026-09-28T09:14:03.000Z",
    "summary": "Reviewer call",
    "description": "Notes, links, agendas",
    "location": "https://meet.test/reviewer-call",
    "creator": {"email": "ops@example.test", "self": True},
    "organizer": {"email": "ops@example.test", "self": True},
    "start": {"dateTime": "2026-09-29T10:00:00+02:00"},
    "end": {"dateTime": "2026-09-29T11:00:00+02:00"},
    "iCalUID": "1a2B3c4D5e6F7g8H9i0J_k4n9vqe7c9k@google.com",
    "sequence": 0,
    "eventType": "default",
}


def _stored_calendar_poll(name):
    """The stored poll trigger named by ``event`` when it watches the
    ``google-calendar.events`` source, or None. A missing selector,
    unconfigured poll triggers, an unknown name and a non-calendar source
    (the generic poll connector owns those) fold together: the caller only
    distinguishes live-vs-fallback, so any storage hiccup folds too —
    sampling never raises for want of infrastructure (see
    docs/connector-coverage-audit.md)."""
    from src.dapier.triggers import poll_triggers

    if not name:
        return None
    try:
        item = poll_triggers.get_item(name)
    except Exception:
        return None
    if not item or str(item.get("source") or "") != "google-calendar.events":
        return None
    return item


def _fetch_calendar_sample(event=None, connection_id=None, limit=DEFAULT_LIMIT):
    """The Google Calendar chip's sample pull: the newest event the stored
    calendar poll watches right now (``source: "live"``), else the newest
    recorded google-calendar run carrying ``event.new`` (``"history"``),
    else a documented example (``"synthetic"``).

    ``event`` names the stored poll trigger; the live pull runs the poll's
    own fetch once against the epoch watermark (read, never advances the
    parked cursor) and wraps the newest item in the envelope a real fire
    would publish. A live fetch that cannot run — no connection, an
    unreachable Calendar API, a trigger that has not fired yet — falls
    through to the recorded/documented sample instead of failing: a sample
    pull shows the payload shape, it never raises.
    """
    from src.dapier.triggers import poll_triggers

    name = str(event or "").strip().lower()
    item = _stored_calendar_poll(name)
    if item is not None:
        try:
            items, _next_cursor = _events_poll_fetch(item, CALENDAR_EPOCH_CURSOR)
            envelope = (poll_triggers.event_for(item, items[-1])
                        if items else None)
        except Exception:
            envelope = None
        if envelope is not None:
            return {
                "sample": trigger_discovery.as_sample(envelope),
                "source": "live",
                "connection_id": item.get("connection_id") or None,
            }
    found = trigger_discovery.history_sample("google-calendar", event="event.new")
    if found is not None:
        return {"sample": found, "source": "history", "connection_id": connection_id}
    return {
        "sample": trigger_discovery.synthetic_sample(
            "google-calendar", "event.new", dict(_CALENDAR_SYNTHETIC_EVENT)),
        "source": "synthetic",
        "connection_id": connection_id,
    }


register_trigger_discovery(TriggerDiscovery(
    connector="google-calendar", label="Google Calendar", kind="sample", resource="",
    fetch=_fetch_calendar_sample))
