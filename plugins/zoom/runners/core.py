"""Shared plumbing for the Zoom action runners: the connection gate, the
request path, the error shape, the projection views steps consume, and the
field-rendering helpers the create/update payload builders share."""
import importlib
import json
import urllib.parse
from datetime import datetime

from src.dapier.engine.actions import base
from src.dapier.engine.actions.templating import render

API_URL = "https://api.zoom.us/v2"
MATCH_MODES = ("contains", "exact")
# The topic-search windows of zoom_find_meeting / zoom_find_webinar:
# ``upcoming`` lists what is scheduled (Zoom's type=upcoming), ``past`` the
# previous_meetings window the participants and cleanup flows want.
FIND_SCOPES = ("upcoming", "past")
# zoom_delete_recording's ``action`` values: ``trash`` sends Zoom's
# action=trash flag (recoverable), ``permanent`` omits it (Zoom's endpoint
# default destroys the recording).
DELETE_RECORDING_ACTIONS = ("trash", "permanent")
# Past-participant listing bounds: one 300-seat page usually suffices, and
# the page/cap ceilings keep a 500-person all-hands inside the run budget.
PARTICIPANTS_PAGE_SIZE = 300
PARTICIPANTS_MAX_PAGES = 3
PARTICIPANTS_CAP = 300


def _zoom_connection(connection_id):
    return base._connected_connection(connection_id)


def _request_json(method, access_token, path, payload=None, *, transport=None):
    """One Zoom API call; returns ``(status, parsed dict)`` without raising on status."""
    transport = transport or base._default_transport
    url = f"{API_URL}{path}"
    headers = {"authorization": f"Bearer {access_token}"}
    body = None
    if payload is not None:
        headers["content-type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode()
    try:
        status, response = transport(method, url, headers=headers, body=body, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"zoom unreachable: {type(exc).__name__}")
    try:
        data = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        data = {}
    return status, data if isinstance(data, dict) else {}


def _get_json(access_token, path, *, transport=None):
    """One Zoom GET; returns ``(status, parsed dict)`` without raising on status."""
    return _request_json("GET", access_token, path, transport=transport)


def _raise_zoom_error(what, status, data):
    detail = str(data.get("message") or "")[:200]
    raise RuntimeError(f"{what} returned HTTP {status}{': ' + detail if detail else ''}")


def _meeting_view(meeting):
    return {
        "id": str(meeting.get("id")),
        "topic": meeting.get("topic"),
        "start_time": meeting.get("start_time"),
        "join_url": meeting.get("join_url"),
        "duration": meeting.get("duration"),
    }


def _topic_matches(topic, wanted, mode):
    topic = str(topic or "").strip().lower()
    if mode == "exact":
        return topic == wanted
    return wanted in topic


def _topic_query(action, event, steps, *, needs):
    """The validated topic search the find runners share: the rendered
    topic (required), the contains/exact match mode, and the upcoming/past
    scope window. ``needs`` is the runner's topic-required error."""
    topic = render(str(action.get("topic") or ""), event, steps).strip()
    if not topic:
        raise ValueError(needs)
    mode = str(action.get("match") or "contains").strip().lower()
    if mode not in MATCH_MODES:
        raise ValueError("match must be one of: contains, exact")
    scope = str(action.get("scope") or "upcoming").strip().lower()
    if scope not in FIND_SCOPES:
        raise ValueError("scope must be one of: upcoming, past")
    return topic, mode, scope


def _recording_view(meeting):
    """One meeting's cloud-recording entry: metadata plus its files."""
    files = []
    for item in meeting.get("recording_files") or []:
        if not isinstance(item, dict):
            continue
        files.append({
            "id": item.get("id"),
            "file_type": item.get("file_type"),
            "file_size": item.get("file_size"),
            "play_url": item.get("play_url"),
            "download_url": item.get("download_url"),
            "recording_start": item.get("recording_start"),
        })
    return {
        "id": str(meeting.get("id")),
        "topic": meeting.get("topic"),
        "start_time": meeting.get("start_time"),
        "duration": meeting.get("duration"),
        "files": files,
    }


def _rendered(action, key, event, steps):
    return render(str(action.get(key) or ""), event, steps).strip()


def _parse_start_time(text):
    """Accept an ISO 8601 datetime (``Z`` or an offset); pass Zoom the original text."""
    try:
        datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        raise ValueError(
            "start_time must be an ISO 8601 datetime (e.g. 2026-10-01T09:00:00Z)"
        ) from None
    return text


def _created_view(meeting):
    """The created meeting as steps consume it: join/start links plus passcode."""
    return {
        "id": str(meeting.get("id")),
        "topic": meeting.get("topic"),
        "start_time": meeting.get("start_time"),
        "duration": meeting.get("duration"),
        "join_url": meeting.get("join_url"),
        "start_url": meeting.get("start_url"),
        "passcode": meeting.get("password"),
    }


def _flag(action, key):
    """Designer boolean fields arrive as "true"/"false" strings."""
    value = action.get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _participant_view(participant):
    """One attendee as steps consume them: who, and when they were in the
    room — the API row trimmed to the keys an attendance email or a sheet
    row templates."""
    return {
        "name": participant.get("name"),
        "user_email": participant.get("user_email"),
        "join_time": participant.get("join_time"),
        "leave_time": participant.get("leave_time"),
        "duration": participant.get("duration"),
    }


def _webinar_view(webinar):
    return {
        "id": str(webinar.get("id")),
        "topic": webinar.get("topic"),
        "start_time": webinar.get("start_time"),
        "join_url": webinar.get("join_url"),
        "duration": webinar.get("duration"),
    }


def _created_webinar_view(webinar):
    """The created webinar as steps consume it: links plus passcode."""
    return {
        "id": str(webinar.get("id")),
        "topic": webinar.get("topic"),
        "start_time": webinar.get("start_time"),
        "duration": webinar.get("duration"),
        "join_url": webinar.get("join_url"),
        "start_url": webinar.get("start_url"),
        "passcode": webinar.get("password"),
    }


def _apply_duration(payload, duration_raw):
    """Validate and apply the whole-minutes duration when one is given."""
    if not duration_raw:
        return
    try:
        duration = int(duration_raw)
    except ValueError:
        raise ValueError("duration must be a whole number of minutes") from None
    if duration <= 0:
        raise ValueError("duration must be a positive number of minutes")
    payload["duration"] = duration


def _apply_settings(payload, settings_raw):
    """Parse and apply the settings JSON object when one is given."""
    if not settings_raw:
        return
    try:
        settings = json.loads(settings_raw)
    except ValueError:
        raise ValueError("settings must be a JSON object") from None
    if not isinstance(settings, dict):
        raise ValueError("settings must be a JSON object")
    payload["settings"] = settings


def _update_fields(action, event, steps):
    """The non-empty PATCH fields the update runners send: topic,
    start_time, duration, timezone, agenda, settings — the create fields,
    minus the create-only defaults. Updating nothing at all is the
    runners' clear error, not an empty PATCH."""
    payload = {}
    topic = _rendered(action, "topic", event, steps)
    if topic:
        payload["topic"] = topic
    start_time = _rendered(action, "start_time", event, steps)
    if start_time:
        payload["start_time"] = _parse_start_time(start_time)
    _apply_duration(payload, _rendered(action, "duration", event, steps))
    for key in ("timezone", "agenda"):
        value = _rendered(action, key, event, steps)
        if value:
            payload[key] = value
    # Rendered like any merge field; literal JSON braces are written {…}.
    _apply_settings(payload, _rendered(action, "settings", event, steps))
    return payload


def _collect_participants(access_token, resource, record_id, *, what,
                          transport=None):
    """The past-event participant pages for
    ``/past_{resource}/{id}/participants``: Zoom's ``next_page_token``
    pagination, stopped at PARTICIPANTS_MAX_PAGES pages or
    PARTICIPANTS_CAP participants, whichever comes first, to keep the run
    inside the lambda budget; ``what`` names the action in error messages."""
    participants = []
    params = {"per_page": PARTICIPANTS_PAGE_SIZE}
    for _page in range(PARTICIPANTS_MAX_PAGES):
        path = (f"/past_{resource}/{urllib.parse.quote(record_id, safe='')}"
                "/participants?" + urllib.parse.urlencode(params))
        status, data = _get_json(access_token, path, transport=transport)
        if status >= 300:
            _raise_zoom_error(what, status, data)
        for entry in data.get("participants") or []:
            if isinstance(entry, dict):
                participants.append(_participant_view(entry))
        next_page = data.get("next_page_token")
        if not next_page or len(participants) >= PARTICIPANTS_CAP:
            break
        params = {**params, "next_page_token": next_page}
    return participants


def _pkg(name):
    """Resolve ``name`` on the package at call time, so the tests'
    ``engine.actions.zoom._zoom_connection`` patches are honored by every
    submodule's runners."""
    return getattr(importlib.import_module(__package__), name)
