"""Google Calendar operations behind the ``/api/agent/calendar/*`` routes
(the `dapier calendar` CLI's backend; the connection token stays
server-side).

Same contract as the Gmail/Drive ops: the account is one Google
connection picked by ``google_accounts.resolve_account``, provider calls
ride the shared ``_request`` transport seam, failures surface with the
provider's redacted detail plus the scope hint, and granted scopes are
honored as-is.

Guest-email safety is explicit, never accidental: every write carries
Google's ``sendUpdates`` and the ops default it to ``none`` — invitations
and updates only go out when the caller names ``all``/``externalOnly``.
Event windows are bounded: ``list_events`` requires ``time_min`` and
``time_max`` (ISO), rejects inverted ranges, and caps the span at
``MAX_WINDOW_DAYS`` so a stray query cannot walk the whole calendar.
Writes accept the raw JSON payload (``event``) for callers that prepared
and reviewed it in advance, or structured fields — never both.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import quote, urlencode

from ...connections import discovery as provider
from ...connections import tokens
from . import google_accounts

CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 250

# The largest single events.list window: bounded time ranges are a brief
# guarantee, and Google caps singleEvents expansion at ~400 days anyway.
MAX_WINDOW_DAYS = 366

DEFAULT_CALENDAR_ID = "primary"

EVENT_FIELDS = ("summary", "description", "location", "start", "end", "attendees",
                "recurrence", "reminders", "attachments", "transparency", "visibility")

_SEND_UPDATES = ("all", "externalOnly", "none")


class CalendarOpsError(Exception):
    """Operator-facing Calendar failure; message must never carry a token."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _status_from(detail):
    match = re.search(r"HTTP (\d{3})", str(detail))
    return int(match.group(1)) if match else 502


def _scope_hint(status, detail):
    if status == 403 and ("insufficient" in detail.lower() or "not authorized" in detail.lower()
                          or "access not configured" in detail.lower()):
        return (" — the connection lacks the Calendar scope for this operation; "
                "update its scopes (dapier connections edit --scopes) and reconnect")
    return ""


def _connection_and_token(account):
    """The resolved Google connection and a live bearer token for it."""
    try:
        connection_id = google_accounts.resolve_account("calendar", account)
    except google_accounts.GoogleAccountError as exc:
        raise CalendarOpsError(str(exc), status=exc.status) from None
    from .. import discovery as discovery_api

    connection, error = discovery_api._load(connection_id, None)
    if error:
        raise CalendarOpsError("Calendar connection not found", status=404)
    try:
        token, _info = tokens.get_access_token(connection)
    except Exception as exc:
        raise CalendarOpsError(
            f"Calendar token unavailable for {connection_id}: {type(exc).__name__}",
            status=502) from None
    return connection_id, connection, token


def _call(token, method, url, payload=None):
    try:
        return provider._request(method, url, token, payload)
    except provider.DiscoveryError as exc:
        status = _status_from(str(exc))
        raise CalendarOpsError(
            f"Calendar {method} failed: {exc}{_scope_hint(status, str(exc))}",
            status=status) from None


def _events_url(calendar_id, suffix="", params=None):
    url = f"{CALENDAR_API_URL}/calendars/{quote(str(calendar_id), safe='')}/events{suffix}"
    if params:
        url += "?" + urlencode(params)
    return url


def _parse_moment(value, name):
    """An ISO 8601 moment (or date) the API accepts; validated, not
    transformed — Google accepts both RFC3339 datetimes and plain dates."""
    text = str(value or "").strip()
    if not text:
        raise CalendarOpsError(f"{name} is required (ISO 8601, e.g. 2026-11-05T09:00:00Z)")
    try:
        if text.endswith("Z"):
            datetime.fromisoformat(text[:-1] + "+00:00")
        elif "T" in text:
            datetime.fromisoformat(text)
        else:
            datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise CalendarOpsError(
            f"{name} is not an ISO 8601 date or datetime: {text!r}") from None
    return text


def _bounded_window(time_min, time_max):
    """(time_min, time_max) as validated ISO strings; inverted or over-long
    windows are rejected — bounded ranges are the API contract here."""
    start = _parse_moment(time_min, "time_min")
    end = _parse_moment(time_max, "time_max")
    try:
        fmt = "%Y-%m-%d" if ("T" not in start and "T" not in end) else None
        if fmt:
            lower = datetime.strptime(start, fmt)
            upper = datetime.strptime(end, fmt)
        else:
            _start = start[:-1] + "+00:00" if start.endswith("Z") else start
            _end = end[:-1] + "+00:00" if end.endswith("Z") else end
            lower = datetime.fromisoformat(_start)
            upper = datetime.fromisoformat(_end)
    except ValueError:
        lower, upper = None, None
    if lower and upper:
        if lower >= upper:
            raise CalendarOpsError("time_min must come before time_max")
        if upper - lower > timedelta(days=MAX_WINDOW_DAYS):
            raise CalendarOpsError(
                f"the window exceeds {MAX_WINDOW_DAYS} days; narrow time_min/time_max")
    return start, end


def _check_send_updates(send_updates):
    value = str(send_updates or "none").strip()
    if value not in _SEND_UPDATES:
        raise CalendarOpsError(
            f"send_updates must be one of {', '.join(_SEND_UPDATES)} (default none)")
    return value


def _event_view(item):
    item = item if isinstance(item, dict) else {}
    start = item.get("start") if isinstance(item.get("start"), dict) else {}
    end = item.get("end") if isinstance(item.get("end"), dict) else {}
    creator = item.get("creator") if isinstance(item.get("creator"), dict) else {}
    organizer = item.get("organizer") if isinstance(item.get("organizer"), dict) else {}
    attendees = []
    for attendee in item.get("attendees") or []:
        if isinstance(attendee, dict):
            attendees.append({
                "email": attendee.get("email"),
                "response_status": attendee.get("responseStatus"),
                "optional": bool(attendee.get("optional")),
            })
    return {
        "event_id": item.get("id"),
        "calendar_id": str(item.get("calendarId") or "") or None,
        "summary": item.get("summary"),
        "description": item.get("description"),
        "location": item.get("location"),
        "status": item.get("status"),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": bool(start.get("date")),
        "timezone": start.get("timeZone"),
        "recurrence": item.get("recurrence") or [],
        "attendees": attendees,
        "reminders": item.get("reminders"),
        "attachments": [
            {"file_url": a.get("fileUrl"), "title": a.get("title"),
             "mime_type": a.get("mimeType")}
            for a in (item.get("attachments") or []) if isinstance(a, dict)
        ],
        "creator": creator.get("email"),
        "organizer": organizer.get("email"),
        "html_link": item.get("htmlLink"),
        "updated": item.get("updated"),
    }


# --- reads ---------------------------------------------------------------------


def accounts(account=None):
    found = google_accounts.list_accounts("calendar")
    return {"accounts": found, "count": len(found)}


def list_calendars(account, max_results=DEFAULT_PAGE_SIZE, page_token=None):
    _connection_id, _connection, token = _connection_and_token(account)
    params = {"maxResults": max(1, min(int(max_results or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)),
              "showHidden": "false"}
    if page_token:
        params["pageToken"] = page_token
    data = _call(token, "GET", CALENDAR_API_URL + "/users/me/calendarList?" + urlencode(params))
    calendars = []
    for item in data.get("items") or []:
        if not (isinstance(item, dict) and item.get("id")):
            continue
        calendars.append({
            "id": item.get("id"),
            "summary": item.get("summary"),
            "timezone": item.get("timeZone"),
            "primary": bool(item.get("primary")),
            "access_role": item.get("accessRole"),
        })
    return {"calendars": calendars, "count": len(calendars),
            "next_page_token": data.get("nextPageToken"), "account": _connection_id}


def list_events(account, calendar_id=DEFAULT_CALENDAR_ID, time_min=None, time_max=None,
                query=None, max_results=DEFAULT_PAGE_SIZE, page_token=None):
    """Events in a bounded window, occurrences expanded in start order."""
    _connection_id, _connection, token = _connection_and_token(account)
    start, end = _bounded_window(time_min, time_max)
    params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": max(1, min(int(max_results or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE)),
        "timeMin": start,
        "timeMax": end,
    }
    if query:
        params["q"] = str(query)
    if page_token:
        params["pageToken"] = page_token
    data = _call(token, "GET", _events_url(calendar_id, "", params))
    events = [_event_view(item) for item in data.get("items") or []
              if isinstance(item, dict) and item.get("id")]
    return {"events": events, "count": len(events),
            "next_page_token": data.get("nextPageToken"),
            "time_min": start, "time_max": end, "account": _connection_id}


def get_event(account, event_id, calendar_id=DEFAULT_CALENDAR_ID):
    _connection_id, _connection, token = _connection_and_token(account)
    data = _call(token, "GET", _events_url(calendar_id, f"/{quote(str(event_id), safe='')}"))
    return _event_view(data)


def freebusy(account, calendar_ids=None, time_min=None, time_max=None):
    """Free/busy for one or more calendars over a bounded window."""
    _connection_id, _connection, token = _connection_and_token(account)
    start, end = _bounded_window(time_min, time_max)
    ids = [str(item).strip() for item in (calendar_ids or [DEFAULT_CALENDAR_ID]) if str(item).strip()]
    if not ids:
        raise CalendarOpsError("at least one calendar id is required")
    data = _call(token, "POST", CALENDAR_API_URL + "/freeBusy",
                 {"timeMin": start, "timeMax": end,
                  "items": [{"id": calendar_id} for calendar_id in ids]})
    calendars = {}
    for calendar_id, block in (data.get("calendars") or {}).items():
        calendars[calendar_id] = {
            "busy": [
                {"start": span.get("start"), "end": span.get("end")}
                for span in block.get("busy") or []
            ],
            "errors": block.get("errors") or [],
        }
    return {"calendars": calendars, "time_min": start, "time_max": end,
            "account": _connection_id}


# --- mutations -----------------------------------------------------------------


def _event_body(summary=None, start=None, end=None, timezone_name=None,
                description=None, location=None, attendees=None, recurrence=None,
                reminders=None, attachments=None, all_day=False, payload=None):
    """The event resource: a reviewed raw payload, or structured fields."""
    if payload is not None:
        if any((summary, start, end, description, location, attendees, recurrence,
                reminders, attachments, timezone_name, all_day)):
            raise CalendarOpsError(
                "pass either the reviewed event payload or structured fields, not both")
        if not isinstance(payload, dict) or not payload:
            raise CalendarOpsError("event payload must be a non-empty JSON object")
        return payload
    if not summary:
        raise CalendarOpsError("a summary is required")
    if not start or not end:
        raise CalendarOpsError("start and end are required (ISO 8601)")
    if all_day:
        start_block = {"date": str(start)[:10]}
        end_block = {"date": str(end)[:10]}
    else:
        start_block = {"dateTime": str(start)}
        end_block = {"dateTime": str(end)}
        if timezone_name:
            start_block["timeZone"] = timezone_name
            end_block["timeZone"] = timezone_name
    body = {"summary": summary, "start": start_block, "end": end_block}
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = [{"email": str(address).strip()}
                             for address in attendees if str(address).strip()]
    if recurrence:
        body["recurrence"] = [str(rule) for rule in recurrence]
    if reminders:
        body["reminders"] = reminders
    if attachments:
        body["attachments"] = [
            {"fileUrl": str(entry.get("file_url") or entry.get("fileUrl") or "")}
            for entry in attachments
            if isinstance(entry, dict) and str(entry.get("file_url") or entry.get("fileUrl") or "").strip()
        ]
    return body


def create_event(account, calendar_id=DEFAULT_CALENDAR_ID, send_updates="none", **fields):
    _connection_id, _connection, token = _connection_and_token(account)
    body = _event_body(**fields)
    params = {"sendUpdates": _check_send_updates(send_updates)}
    data = _call(token, "POST", _events_url(calendar_id, "", params), body)
    return _event_view(data)


def update_event(account, event_id, calendar_id=DEFAULT_CALENDAR_ID, send_updates="none",
                 **fields):
    """PATCH one event; only the given structured fields change (a reviewed
    raw payload replaces via the same PATCH semantics)."""
    _connection_id, _connection, token = _connection_and_token(account)
    body = _event_body(**fields)
    params = {"sendUpdates": _check_send_updates(send_updates)}
    url = _events_url(calendar_id, f"/{quote(str(event_id), safe='')}", params)
    data = _call(token, "PATCH", url, body)
    return _event_view(data)


def delete_event(account, event_id, calendar_id=DEFAULT_CALENDAR_ID, send_updates="none"):
    _connection_id, _connection, token = _connection_and_token(account)
    params = {"sendUpdates": _check_send_updates(send_updates)}
    url = _events_url(calendar_id, f"/{quote(str(event_id), safe='')}", params)
    _call(token, "DELETE", url)
    return {"deleted": event_id, "calendar_id": calendar_id,
            "send_updates": _check_send_updates(send_updates), "account": _connection_id}
