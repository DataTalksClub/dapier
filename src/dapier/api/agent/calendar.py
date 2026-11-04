"""Operator Google Calendar proxy: the `dapier calendar` CLI's routes over
the ops layer. Same gating/audit/account rules as the Gmail/Drive routes;
writes carry Google's ``sendUpdates`` explicitly (default ``none`` — no
accidental guest email)."""

import json
import re

from . import calendar_ops
from .common import _LateBinding, _json_response
from .google_accounts import GoogleAccountError

require_operator = _LateBinding("require_operator")
audit = _LateBinding("audit")


def _error(exc):
    status = getattr(exc, "status", 400) or 400
    message = str(exc) or "Calendar request failed"
    return _json_response(status, {"error": message})


def _account(event, body=None):
    query = event.get("queryStringParameters") or {}
    raw = query.get("account")
    if isinstance(body, dict):
        raw = raw or body.get("account")
    return str(raw or "").strip() or None


def _json_body(event):
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, TypeError):
        return None
    return body if isinstance(body, dict) else None


def _audit(connection_id, action, subject):
    audit.emit(connection_id, action, subject, outcome="ok")


def accounts_api(event):
    subject, error = require_operator(event, "calendar.accounts")
    if error:
        return error
    return _json_response(200, calendar_ops.accounts())


def list_calendars_api(event):
    subject, error = require_operator(event, "calendar.list_calendars")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = calendar_ops.list_calendars(
            _account(event),
            max_results=int(query.get("max") or 0) or calendar_ops.DEFAULT_PAGE_SIZE,
            page_token=query.get("page_token"))
    except (calendar_ops.CalendarOpsError, GoogleAccountError, ValueError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "calendar", "calendar.list_calendars", subject)
    return _json_response(200, payload)


def _event_fields(body):
    """Structured event fields from a request body (the CLI's write verbs)."""
    return {
        "summary": body.get("summary"),
        "start": body.get("start"),
        "end": body.get("end"),
        "timezone_name": body.get("timezone"),
        "description": body.get("description"),
        "location": body.get("location"),
        "attendees": body.get("attendees") or [],
        "recurrence": body.get("recurrence") or [],
        "reminders": body.get("reminders"),
        "attachments": body.get("attachments") or [],
        "all_day": bool(body.get("all_day")),
        "payload": body.get("event"),
    }


def list_events_api(event):
    subject, error = require_operator(event, "calendar.list_events")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = calendar_ops.list_events(
            _account(event),
            calendar_id=query.get("calendar") or calendar_ops.DEFAULT_CALENDAR_ID,
            time_min=query.get("time_min"), time_max=query.get("time_max"),
            query=query.get("q") or query.get("query"),
            max_results=int(query.get("max") or 0) or calendar_ops.DEFAULT_PAGE_SIZE,
            page_token=query.get("page_token"))
    except (calendar_ops.CalendarOpsError, GoogleAccountError, ValueError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "calendar", "calendar.list_events", subject)
    return _json_response(200, payload)


def get_event_api(event, event_id):
    subject, error = require_operator(event, "calendar.get_event")
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    try:
        payload = calendar_ops.get_event(
            _account(event), event_id,
            calendar_id=query.get("calendar") or calendar_ops.DEFAULT_CALENDAR_ID)
    except (calendar_ops.CalendarOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("organizer") or "calendar", "calendar.get_event", subject)
    return _json_response(200, payload)


def freebusy_api(event):
    subject, error = require_operator(event, "calendar.freebusy")
    if error:
        return error
    body = _json_body(event) or {}
    query = event.get("queryStringParameters") or {}
    try:
        payload = calendar_ops.freebusy(
            _account(event, body),
            calendar_ids=body.get("calendar_ids")
            or [item for item in (query.get("calendar") or "").split(",") if item]
            or None,
            time_min=body.get("time_min") or query.get("time_min"),
            time_max=body.get("time_max") or query.get("time_max"))
    except (calendar_ops.CalendarOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "calendar", "calendar.freebusy", subject)
    return _json_response(200, payload)


def create_event_api(event):
    subject, error = require_operator(event, "calendar.create_event")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = calendar_ops.create_event(
            _account(event, body),
            calendar_id=body.get("calendar") or calendar_ops.DEFAULT_CALENDAR_ID,
            send_updates=body.get("send_updates") or "none",
            **_event_fields(body))
    except (calendar_ops.CalendarOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("organizer") or "calendar", "calendar.create_event", subject)
    return _json_response(200, payload)


_EVENT_ID = r"([A-Za-z0-9_-]+)"


def update_event_api(event, event_id):
    subject, error = require_operator(event, "calendar.update_event")
    if error:
        return error
    body = _json_body(event)
    if body is None:
        return _json_response(400, {"error": "Invalid JSON body"})
    try:
        payload = calendar_ops.update_event(
            _account(event, body), event_id,
            calendar_id=body.get("calendar") or calendar_ops.DEFAULT_CALENDAR_ID,
            send_updates=body.get("send_updates") or "none",
            **_event_fields(body))
    except (calendar_ops.CalendarOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("organizer") or "calendar", "calendar.update_event", subject)
    return _json_response(200, payload)


def delete_event_api(event, event_id):
    subject, error = require_operator(event, "calendar.delete_event")
    if error:
        return error
    body = _json_body(event) or {}
    try:
        payload = calendar_ops.delete_event(
            _account(event, body), event_id,
            calendar_id=body.get("calendar") or calendar_ops.DEFAULT_CALENDAR_ID,
            send_updates=body.get("send_updates") or "none")
    except (calendar_ops.CalendarOpsError, GoogleAccountError) as exc:
        return _error(exc)
    _audit(payload.get("account") or "calendar", "calendar.delete_event", subject)
    return _json_response(200, payload)


def route_calendar(event, method, path):
    """Return a response for Calendar agent routes, or None if unmatched."""
    if path == "/api/agent/calendar/accounts" and method == "GET":
        return accounts_api(event)
    if path == "/api/agent/calendar/calendars" and method == "GET":
        return list_calendars_api(event)
    if path == "/api/agent/calendar/events" and method == "GET":
        return list_events_api(event)
    if path == "/api/agent/calendar/events" and method == "POST":
        return create_event_api(event)
    if path == "/api/agent/calendar/freebusy" and method == "POST":
        return freebusy_api(event)
    match = re.fullmatch(r"/api/agent/calendar/events/" + _EVENT_ID, path)
    if match and method == "GET":
        return get_event_api(event, match.group(1))
    if match and method == "PATCH":
        return update_event_api(event, match.group(1))
    if match and method == "DELETE":
        return delete_event_api(event, match.group(1))
    return None
