"""Google Calendar actions through a Google connection: create an event
(detailed or quick-add), find events, update one, delete one.

``calendar_create_event`` writes a full event (summary, start/end,
description, location, attendees) into a calendar; ``calendar_quick_add``
is the one-text-line shortcut. ``calendar_find_events`` searches a
calendar (text query over a time window) and can create the event when
nothing matches, mirroring ``sheets_find_row``'s find-or-create;
``calendar_update_event`` patches the provided fields of one event and
``calendar_delete_event`` removes it.

Times are ISO: a bare ``YYYY-MM-DD`` is an all-day date (Google's ``date``
field), anything else a ``dateTime`` (with the optional ``timezone`` IANA
name attached). Start and end must agree on the style, since Google
rejects mixed event times.
"""
import json
import re
import urllib.parse
from datetime import datetime, timedelta, timezone

from ...connections import tokens
from . import base
from .templating import render

CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"

# Bare calendar days (all-day events) vs datetimes; the match decides the
# Google ``date`` vs ``dateTime`` field, which never mix in one event.
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}$")

# The matches listed under ``events`` are bounded so a wide query cannot
# flood downstream payloads; ``count`` stays the API's returned count.
FIND_EVENTS_CAP = 10
FIND_MAX_RESULTS = 50
# The find window when the action names neither bound: yesterday through
# the next quarter, so "find" mostly sees upcoming events.
FIND_DEFAULT_DAYS_BACK = 1
FIND_DEFAULT_DAYS_AHEAD = 90


def _api(method, url, token, payload=None, *, transport=None):
    """One Calendar API call; returns parsed JSON ({} on an empty body)."""
    transport = transport or base._default_transport
    headers = {"authorization": f"Bearer {token}"}
    body = None
    if payload is not None:
        headers["content-type"] = "application/json"
        body = json.dumps(payload).encode()
    try:
        status, response = transport(method, url, headers=headers, body=body,
                                     timeout=15)
    except Exception as exc:
        raise RuntimeError(f"google calendar unreachable: {type(exc).__name__}")
    if status >= 300:
        detail = ""
        try:
            error = json.loads(response.decode() or "{}").get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = f" ({str(error['message'])[:200]})"
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"google calendar {method} returned HTTP {status}{detail}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def _events_url(calendar_id, suffix=""):
    """The calendar's events endpoint with the id quoted whole (the ``primary``
    alias survives quoting — it has no reserved characters)."""
    quoted = urllib.parse.quote(str(calendar_id), safe="")
    return f"{CALENDAR_API_URL}/calendars/{quoted}/events{suffix}"


def _field(action, key, event, steps=None):
    """One action field, template-rendered and trimmed."""
    return render(str(action.get(key) or ""), event, steps).strip()


def _event_time(value, timezone_name):
    """One time boundary as Google's event-time dict, or None when absent."""
    text = str(value or "").strip()
    if not text:
        return None
    if _DATE_ONLY.fullmatch(text):
        return {"date": text}
    moment = {"dateTime": text}
    if timezone_name:
        moment["timeZone"] = timezone_name
    return moment


def _event_times(action, event, steps=None):
    """The event's ``start``/``end`` dicts; raises when the styles mix."""
    timezone_name = _field(action, "timezone", event, steps)
    start = _event_time(_field(action, "start", event, steps), timezone_name)
    end = _event_time(_field(action, "end", event, steps), timezone_name)
    if start and end and (("date" in start) != ("date" in end)):
        raise ValueError("start and end must both be dates (YYYY-MM-DD) or both datetimes")
    return start, end


def _attendees_field(action, event, steps=None):
    """The ``attendees`` key as a Google attendees list, or None.

    Accepts a JSON array (of email strings or ``{"email": …}`` objects) or a
    comma-separated address list; entries without an address are dropped.
    """
    raw = action.get("attendees")
    if raw is None or str(raw).strip() == "":
        return None
    if isinstance(raw, str):
        rendered = render(raw, event, steps).strip()
        try:
            raw = json.loads(rendered)
        except ValueError:
            raw = [part.strip() for part in rendered.split(",")]
    if not isinstance(raw, list):
        raise ValueError("attendees must be a JSON array or a comma-separated list of emails")
    attendees = []
    for item in raw:
        email = (item.get("email") if isinstance(item, dict) else item) or ""
        email = str(email).strip()
        if email:
            attendees.append({"email": email})
    return attendees or None


def _project_event(item):
    """One API event as the step output shows it; ``start``/``end`` flatten
    to their raw strings and ``all_day`` flags the bare-date shape."""
    item = item if isinstance(item, dict) else {}
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    return {
        "event_id": item.get("id"),
        "summary": item.get("summary"),
        "description": item.get("description"),
        "location": item.get("location"),
        "status": item.get("status"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": bool(start.get("date")),
        "html_link": item.get("htmlLink"),
        "created": item.get("created"),
        "updated": item.get("updated"),
    }


def _create_body(action, event, steps=None):
    """The event resource ``calendar_create_event`` (or the create-if-missing
    find) posts: summary plus the optional fields that are set."""
    summary = _field(action, "summary", event, steps)
    if not summary:
        raise ValueError("calendar_create_event requires a summary")
    start, end = _event_times(action, event, steps)
    if not start or not end:
        raise ValueError("calendar_create_event requires start and end")
    body = {"summary": summary, "start": start, "end": end}
    description = _field(action, "description", event, steps)
    if description:
        body["description"] = description
    location = _field(action, "location", event, steps)
    if location:
        body["location"] = location
    attendees = _attendees_field(action, event, steps)
    if attendees:
        body["attendees"] = attendees
    return body


def run_calendar_create_event(action, event, *, transport=None, steps=None):
    """Create one event in a calendar (Create Detailed Event)."""
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    calendar_id = _field(action, "calendar_id", event, steps)
    if not calendar_id:
        raise ValueError("calendar_create_event requires a calendar_id")
    body = _create_body(action, event, steps)
    result = _api("POST", _events_url(calendar_id), access_token, body,
                  transport=transport)
    return _project_event(result)


def run_calendar_quick_add(action, event, *, transport=None, steps=None):
    """Create an event from one line of text (Quick Add Event)."""
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    calendar_id = _field(action, "calendar_id", event, steps)
    text = _field(action, "text", event, steps)
    if not calendar_id:
        raise ValueError("calendar_quick_add requires a calendar_id")
    if not text:
        raise ValueError("calendar_quick_add requires text")
    url = _events_url(calendar_id, "/quickAdd") + "?" + urllib.parse.urlencode(
        {"text": text})
    result = _api("POST", url, access_token, transport=transport)
    return _project_event(result)


def _iso_moment(days_from_now):
    return (datetime.now(timezone.utc) + timedelta(days=days_from_now)) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")


def run_calendar_find_events(action, event, *, transport=None, steps=None):
    """Find events in a calendar (Find Event), optionally creating the first
    one when the search misses (Find or Create Event).

    The search is the Calendar API's text ``q`` over a time window: the
    optional ``time_min``/``time_max`` bound it, defaulting to yesterday
    through the next quarter. The output carries the first match under
    ``event``, how many the API returned under ``count``, every match under
    ``events`` (bounded at :data:`FIND_EVENTS_CAP`), and ``found`` — which
    flips to ``created: True`` on a find-or-create hit. With
    ``create_if_missing`` on, a miss needs ``summary``/``start``/``end``
    (plus the optional fields) and posts the event instead.
    """
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    calendar_id = _field(action, "calendar_id", event, steps)
    if not calendar_id:
        raise ValueError("calendar_find_events requires a calendar_id")
    params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": str(FIND_MAX_RESULTS),
        "timeMin": _field(action, "time_min", event, steps)
        or _iso_moment(-FIND_DEFAULT_DAYS_BACK),
        "timeMax": _field(action, "time_max", event, steps)
        or _iso_moment(FIND_DEFAULT_DAYS_AHEAD),
    }
    query = _field(action, "query", event, steps)
    if query:
        params["q"] = query
    result = _api("GET", _events_url(calendar_id) + "?" + urllib.parse.urlencode(params),
                  access_token, transport=transport)
    items = [item for item in result.get("items") or []
             if isinstance(item, dict) and item.get("id")]
    if items:
        projected = [_project_event(item) for item in items]
        return {"found": True, "created": False, "count": len(projected),
                "event": projected[0], "events": projected[:FIND_EVENTS_CAP]}
    if str(action.get("create_if_missing") or "").strip().lower() in ("true", "1", "yes", "on"):
        body = _create_body(action, event, steps)
        created = _api("POST", _events_url(calendar_id), access_token, body,
                       transport=transport)
        return {"found": False, "created": True, "count": 1,
                "event": _project_event(created), "events": [_project_event(created)]}
    return {"found": False, "created": False, "count": 0, "event": None, "events": []}


_UPDATE_FIELDS = ("summary", "description", "location")


def run_calendar_update_event(action, event, *, transport=None, steps=None):
    """Patch the provided fields of one event (Update Event)."""
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    calendar_id = _field(action, "calendar_id", event, steps)
    event_id = _field(action, "event_id", event, steps)
    if not calendar_id:
        raise ValueError("calendar_update_event requires a calendar_id")
    if not event_id:
        raise ValueError("calendar_update_event requires an event_id")
    body = {}
    for key in _UPDATE_FIELDS:
        value = _field(action, key, event, steps)
        if value:
            body[key] = value
    start, end = _event_times(action, event, steps)
    if start:
        body["start"] = start
    if end:
        body["end"] = end
    attendees = _attendees_field(action, event, steps)
    if attendees:
        body["attendees"] = attendees
    if not body:
        raise ValueError("calendar_update_event needs at least one field to change")
    result = _api("PATCH", _events_url(calendar_id, "/" + urllib.parse.quote(event_id, safe="")),
                  access_token, body, transport=transport)
    return _project_event(result)


def run_calendar_delete_event(action, event, *, transport=None, steps=None):
    """Delete one event from a calendar (Delete Event)."""
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    calendar_id = _field(action, "calendar_id", event, steps)
    event_id = _field(action, "event_id", event, steps)
    if not calendar_id:
        raise ValueError("calendar_delete_event requires a calendar_id")
    if not event_id:
        raise ValueError("calendar_delete_event requires an event_id")
    _api("DELETE", _events_url(calendar_id, "/" + urllib.parse.quote(event_id, safe="")),
         access_token, transport=transport)
    return {"event_id": event_id, "deleted": True}
