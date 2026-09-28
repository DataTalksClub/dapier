"""zoom_find_meeting / zoom_find_recording: look up Zoom meetings and cloud
recordings through an OAuth connection. zoom_create_meeting: create one
(scheduled or instant) meeting on the connected account.
zoom_update_meeting / zoom_add_registrant: reschedule one and register one
attendee on it. zoom_delete_meeting: remove one — or one occurrence of a
recurring one. zoom_list_past_participants: list who attended one past
meeting. The webinar runners (zoom_create_webinar, zoom_update_webinar,
zoom_find_webinar, zoom_add_webinar_registrant, zoom_delete_webinar,
zoom_list_past_webinar_participants) mirror the meeting shapes
one level up on Zoom's REST API."""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from ...connections import tokens
from . import base
from .templating import render

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


def run_zoom_find_meeting(action, event, *, transport=None, steps=None):
    """Find a Zoom meeting by id, or by topic across its ``scope`` window.

    A missing meeting is a verdict (``found: False``), not an error — Zoom's
    404 for a dead meeting id and an unmatched topic both land there — so a
    workflow can branch on the outcome. The topic path searches the
    ``scope`` listing: upcoming (scheduled) meetings by default, past ones
    with ``scope: past`` (the previous_meetings window the participants and
    cleanup flows want). On the upcoming topic path,
    ``create_if_missing`` turns the miss into find-or-create (Zapier's Find
    or Create Meeting): the meeting is created from ``topic`` and the shared
    create fields (start_time, duration, timezone, agenda, settings — the
    same rules zoom_create_meeting applies) and the output reports
    ``created: True``. The id path never creates: a dead meeting id is an
    explicit reference, not a name to reserve.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = render(str(action.get("meeting_id") or ""), event, steps).strip()
    if meeting_id:
        status, data = _get_json(
            access_token,
            f"/meetings/{urllib.parse.quote(meeting_id, safe='')}",
            transport=transport,
        )
        if status == 404:
            return {"found": False, "meeting": None}
        if status >= 300:
            _raise_zoom_error("zoom meeting lookup", status, data)
        return {"found": True, "meeting": _meeting_view(data)}
    topic = render(str(action.get("topic") or ""), event, steps).strip()
    if not topic:
        raise ValueError("zoom_find_meeting needs meeting_id or topic")
    mode = str(action.get("match") or "contains").strip().lower()
    if mode not in MATCH_MODES:
        raise ValueError("match must be one of: contains, exact")
    scope = str(action.get("scope") or "upcoming").strip().lower()
    if scope not in FIND_SCOPES:
        raise ValueError("scope must be one of: upcoming, past")
    if scope == "past" and _flag(action, "create_if_missing"):
        raise ValueError(
            "zoom_find_meeting create_if_missing only applies to the "
            "upcoming scope — a past meeting cannot be created")
    listing_type = "previous_meetings" if scope == "past" else "upcoming"
    status, data = _get_json(
        access_token,
        "/users/me/meetings?" + urllib.parse.urlencode(
            {"type": listing_type, "per_page": 300}),
        transport=transport,
    )
    if status >= 300:
        _raise_zoom_error("zoom meeting list", status, data)
    for meeting in data.get("meetings") or []:
        if isinstance(meeting, dict) and _topic_matches(meeting.get("topic"), topic.lower(), mode):
            return {"found": True, "meeting": _meeting_view(meeting),
                    "matched_by": "topic", "scope": scope}
    if not _flag(action, "create_if_missing"):
        return {"found": False, "meeting": None, "matched_by": "topic", "scope": scope}
    payload = _create_meeting_payload(action, event, steps,
                                      what="zoom_find_meeting create_if_missing")
    created = _create_meeting(access_token, payload, transport=transport)
    return {"found": False, "created": True,
            "scheduled": payload.get("type") == 2,
            "meeting": _created_view(created)}



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


def run_zoom_find_recording(action, event, *, transport=None, steps=None):
    """Find Zoom cloud recordings: by meeting id, by topic, or the latest.

    The id path hits the meeting's own recordings endpoint (Zoom's 404 for
    a dead id — the listing only covers the last 30 days — is a verdict,
    ``found: False``, not an error). Without an id the 30-day listing is
    consulted, filtered by ``topic`` (``match`` contains/exact) when given,
    otherwise left whole so the most recent recording tops the list — the
    "share my latest recording" chain needs no arguments at all.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = render(str(action.get("meeting_id") or ""), event, steps).strip()
    if meeting_id:
        status, data = _get_json(
            access_token,
            f"/meetings/{urllib.parse.quote(meeting_id, safe='')}/recordings",
            transport=transport,
        )
        if status == 404:
            return {"found": False, "recording": None, "recordings": [], "count": 0}
        if status >= 300:
            _raise_zoom_error("zoom recording lookup", status, data)
        recording = _recording_view(data)
        return {"found": True, "recording": recording, "recordings": [recording], "count": 1}
    mode = str(action.get("match") or "contains").strip().lower()
    if mode not in MATCH_MODES:
        raise ValueError("match must be one of: contains, exact")
    since = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    status, data = _get_json(
        access_token,
        "/users/me/recordings?" + urllib.parse.urlencode({"from": since, "per_page": 300}),
        transport=transport,
    )
    if status >= 300:
        _raise_zoom_error("zoom recording list", status, data)
    recordings = [
        _recording_view(meeting)
        for meeting in data.get("meetings") or []
        if isinstance(meeting, dict) and meeting.get("id")
    ]
    topic = render(str(action.get("topic") or ""), event, steps).strip()
    if topic:
        recordings = [r for r in recordings if _topic_matches(r.get("topic"), topic.lower(), mode)]
    return {
        "found": bool(recordings),
        "recording": recordings[0] if recordings else None,
        "recordings": recordings,
        "count": len(recordings),
        **({"matched_by": "topic"} if topic else {}),
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


def _create_meeting_payload(action, event, steps, *, what="zoom_create_meeting"):
    """The POST /users/me/meetings payload from the shared create fields.

    A rendered ``start_time`` schedules the meeting (Zoom type 2 — the common
    "schedule a session from a form/email" chain); without one Zoom creates an
    instant meeting (type 1). ``settings`` takes a JSON object of Zoom meeting
    settings (join_before_host, waiting_room, …) verbatim. ``what`` names the
    action in error messages — zoom_find_meeting's create-if-missing branch
    shares this builder.
    """
    topic = _rendered(action, "topic", event, steps)
    if not topic:
        raise ValueError(f"{what} requires topic")
    payload = {"topic": topic}
    start_time = _rendered(action, "start_time", event, steps)
    if start_time:
        payload["type"] = 2
        payload["start_time"] = _parse_start_time(start_time)
        duration_raw = _rendered(action, "duration", event, steps) or "60"
    else:
        payload["type"] = 1
        duration_raw = _rendered(action, "duration", event, steps)
    if duration_raw:
        try:
            duration = int(duration_raw)
        except ValueError:
            raise ValueError("duration must be a whole number of minutes") from None
        if duration <= 0:
            raise ValueError("duration must be a positive number of minutes")
        payload["duration"] = duration
    for key in ("timezone", "agenda"):
        value = _rendered(action, key, event, steps)
        if value:
            payload[key] = value
    # Rendered like any merge field; literal JSON braces are written {{…}}.
    settings_raw = _rendered(action, "settings", event, steps)
    if settings_raw:
        try:
            settings = json.loads(settings_raw)
        except ValueError:
            raise ValueError("settings must be a JSON object") from None
        if not isinstance(settings, dict):
            raise ValueError("settings must be a JSON object")
        payload["settings"] = settings
    return payload


def _create_meeting(access_token, payload, *, transport=None):
    """One POST /users/me/meetings; the created meeting's API object."""
    status, data = _request_json("POST", access_token, "/users/me/meetings",
                                 payload, transport=transport)
    if status >= 300:
        _raise_zoom_error("zoom create meeting", status, data)
    return data


def run_zoom_create_meeting(action, event, *, transport=None, steps=None):
    """Create a meeting on the connected user's account (Zapier's top Zoom action).

    A rendered ``start_time`` schedules the meeting (Zoom type 2 — the common
    "schedule a session from a form/email" chain); without one Zoom creates an
    instant meeting (type 1). ``settings`` takes a JSON object of Zoom meeting
    settings (join_before_host, waiting_room, …) verbatim.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    payload = _create_meeting_payload(action, event, steps)
    data = _create_meeting(access_token, payload, transport=transport)
    return {"created": True, "scheduled": payload.get("type") == 2,
            "meeting": _created_view(data)}


# --- zoom_update_meeting / zoom_add_registrant: reschedule one, seat one -------


def run_zoom_update_meeting(action, event, *, transport=None, steps=None):
    """Update one meeting's schedule or metadata (PATCH /meetings/{id}).

    Only the fields that render non-empty are sent — Zoom patches those and
    leaves the rest of the meeting untouched, so a workflow can move one
    occurrence's start time without restating its agenda. The field rules
    are create_meeting's: a rendered ``start_time`` must parse as ISO 8601,
    ``duration`` is a positive whole number of minutes, ``settings`` is a
    JSON object. Updating nothing at all is a clear error, not an empty
    PATCH. Zoom answers 204 with no body, so the output carries the meeting
    id and what was sent: ``{updated: true, meeting_id, updated_fields}``.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = _rendered(action, "meeting_id", event, steps)
    if not meeting_id:
        raise ValueError("zoom_update_meeting requires meeting_id")
    payload = {}
    topic = _rendered(action, "topic", event, steps)
    if topic:
        payload["topic"] = topic
    start_time = _rendered(action, "start_time", event, steps)
    if start_time:
        payload["start_time"] = _parse_start_time(start_time)
    duration_raw = _rendered(action, "duration", event, steps)
    if duration_raw:
        try:
            duration = int(duration_raw)
        except ValueError:
            raise ValueError("duration must be a whole number of minutes") from None
        if duration <= 0:
            raise ValueError("duration must be a positive number of minutes")
        payload["duration"] = duration
    for key in ("timezone", "agenda"):
        value = _rendered(action, key, event, steps)
        if value:
            payload[key] = value
    # Rendered like any merge field; literal JSON braces are written {{…}}.
    settings_raw = _rendered(action, "settings", event, steps)
    if settings_raw:
        try:
            settings = json.loads(settings_raw)
        except ValueError:
            raise ValueError("settings must be a JSON object") from None
        if not isinstance(settings, dict):
            raise ValueError("settings must be a JSON object")
        payload["settings"] = settings
    if not payload:
        raise ValueError(
            "zoom_update_meeting needs at least one of: topic, start_time, "
            "duration, timezone, agenda, settings")
    status, data = _request_json(
        "PATCH", access_token,
        f"/meetings/{urllib.parse.quote(meeting_id, safe='')}",
        payload, transport=transport)
    if status >= 300:
        _raise_zoom_error("zoom update meeting", status, data)
    return {"updated": True, "meeting_id": str(meeting_id),
            "updated_fields": sorted(payload)}


def run_zoom_add_registrant(action, event, *, transport=None, steps=None):
    """Register one attendee for a meeting that requires registration
    (POST /meetings/{id}/registrants).

    The response's ``join_url`` is the registrant's personalized link — the
    thing an invite email templates — unique per registrant, unlike the
    meeting's public join URL. The meeting must have registration enabled
    (Zoom answers HTTP 4xx otherwise, surfacing its message). Output:
    ``{registered: true, meeting_id, registrant_id, join_url}``.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = _rendered(action, "meeting_id", event, steps)
    if not meeting_id:
        raise ValueError("zoom_add_registrant requires meeting_id")
    email = _rendered(action, "email", event, steps)
    if not email:
        raise ValueError("zoom_add_registrant requires email")
    payload = {"email": email}
    for key in ("first_name", "last_name"):
        value = _rendered(action, key, event, steps)
        if value:
            payload[key] = value
    status, data = _request_json(
        "POST", access_token,
        f"/meetings/{urllib.parse.quote(meeting_id, safe='')}/registrants",
        payload, transport=transport)
    if status >= 300:
        _raise_zoom_error("zoom add registrant", status, data)
    return {
        "registered": True,
        "meeting_id": str(meeting_id),
        "registrant_id": str(data.get("registrant_id") or ""),
        "join_url": data.get("join_url"),
    }


def run_zoom_delete_meeting(action, event, *, transport=None, steps=None):
    """Delete one meeting (DELETE /meetings/{id}).

    ``occurrence_id`` scopes the delete to one occurrence of a recurring
    meeting — without it the whole series goes away, so the field exists to
    make that choice visible rather than implicit. Zoom answers 204 with no
    body; a meeting id that is already gone is Zoom's 404, an error like any
    provider refusal — branch on zoom_find_meeting's ``found`` verdict first
    when the meeting may not exist. Output: ``{deleted: true, meeting_id}``
    (plus ``occurrence_id`` when one was sent).
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = _rendered(action, "meeting_id", event, steps)
    if not meeting_id:
        raise ValueError("zoom_delete_meeting requires meeting_id")
    path = f"/meetings/{urllib.parse.quote(meeting_id, safe='')}"
    occurrence_id = _rendered(action, "occurrence_id", event, steps)
    if occurrence_id:
        path += "?" + urllib.parse.urlencode(
            {"occurrence_id": occurrence_id})
    status, data = _request_json("DELETE", access_token, path, transport=transport)
    if status >= 300:
        _raise_zoom_error("zoom delete meeting", status, data)
    output = {"deleted": True, "meeting_id": str(meeting_id)}
    if occurrence_id:
        output["occurrence_id"] = occurrence_id
    return output


# --- zoom_delete_recording: one cloud recording, trashed or destroyed ----------


def run_zoom_delete_recording(action, event, *, transport=None, steps=None):
    """Delete one meeting's cloud recording (DELETE
    /meetings/{id}/recordings; Zapier's Delete Recording).

    ``meeting_id`` is the meeting's id — the same field
    zoom_find_recording looks up and the recording.completed event (webhook
    or the zoom.recordings poll) publishes. ``action`` picks how the
    recording goes: ``trash`` (the default, the recoverable path) sends
    Zoom's ``action=trash`` query flag, ``permanent`` omits it — Zoom's own
    endpoint default, which destroys the recording and its files outright.
    Zoom answers 204 with no body; a dead meeting id is its 404, an error
    like any provider refusal — branch on zoom_find_recording's ``found``
    verdict first when the recording may not exist. Output: ``{deleted:
    true, meeting_id, action}``.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = _rendered(action, "meeting_id", event, steps)
    if not meeting_id:
        raise ValueError("zoom_delete_recording requires meeting_id")
    mode = str(action.get("action") or "trash").strip().lower()
    if mode not in DELETE_RECORDING_ACTIONS:
        raise ValueError(
            "zoom_delete_recording action must be one of: "
            + ", ".join(DELETE_RECORDING_ACTIONS))
    path = f"/meetings/{urllib.parse.quote(meeting_id, safe='')}/recordings"
    if mode == "trash":
        path += "?" + urllib.parse.urlencode({"action": "trash"})
    status, data = _request_json("DELETE", access_token, path, transport=transport)
    if status >= 300:
        _raise_zoom_error("zoom delete recording", status, data)
    return {"deleted": True, "meeting_id": str(meeting_id), "action": mode}


# --- zoom_list_past_participants: who attended one past meeting -----------------


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


def run_zoom_list_past_participants(action, event, *, transport=None, steps=None):
    """List who attended one past meeting (GET /past_meetings/{id}/participants).

    Pair with the meeting.ended trigger — its meeting uuid feeds
    ``meeting_id`` — and template the participants (name, email, join/leave
    times, duration) into an attendance email or a sheet row. Pagination
    follows Zoom's ``next_page_token`` and stops at PARTICIPANTS_MAX_PAGES
    pages or PARTICIPANTS_CAP participants, whichever comes first, to keep
    the run inside the lambda budget; ``count`` is what was fetched. A dead
    meeting id is Zoom's 404 — an error like any provider refusal; branch on
    zoom_find_meeting's ``found`` verdict first when the meeting may not
    exist. Output: ``{participants, count, meeting_id}``.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = _rendered(action, "meeting_id", event, steps)
    if not meeting_id:
        raise ValueError("zoom_list_past_participants requires meeting_id")
    participants = []
    params = {"per_page": PARTICIPANTS_PAGE_SIZE}
    for _page in range(PARTICIPANTS_MAX_PAGES):
        path = (f"/past_meetings/{urllib.parse.quote(meeting_id, safe='')}"
                "/participants?" + urllib.parse.urlencode(params))
        status, data = _get_json(access_token, path, transport=transport)
        if status >= 300:
            _raise_zoom_error("zoom participant list", status, data)
        for entry in data.get("participants") or []:
            if isinstance(entry, dict):
                participants.append(_participant_view(entry))
        next_page = data.get("next_page_token")
        if not next_page or len(participants) >= PARTICIPANTS_CAP:
            break
        params = {**params, "next_page_token": next_page}
    return {"participants": participants, "count": len(participants),
            "meeting_id": str(meeting_id)}


# --- webinars: Zapier's core webinar set, mirrored on the meeting actions -----
#
# Zoom serves webinars through the same REST shapes one level up: POST
# /users/me/webinars creates, PATCH /webinars/{id} updates, GET
# /users/me/webinars lists (type=scheduled|upcoming|previous_meetings), and
# POST /webinars/{id}/registrants seats one person — each with the
# transport-injected seam the meeting runners use. Webinar ids are NOT
# meeting ids: a registrant added to the wrong kind of id is Zoom's 4xx.


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


def _create_webinar_payload(action, event, steps, *, what="zoom_create_webinar"):
    """The POST /users/me/webinars payload from the shared create fields.

    Webinars have no instant kind (Zapier's is the same): a rendered
    ``start_time`` schedules a fixed webinar (Zoom type 5 — the common
    "schedule a session from a form/email" chain); without one the webinar
    is recurring with no fixed time (Zoom type 6). ``settings`` takes a JSON
    object of Zoom webinar settings verbatim.
    """
    topic = _rendered(action, "topic", event, steps)
    if not topic:
        raise ValueError(f"{what} requires topic")
    payload = {"topic": topic}
    start_time = _rendered(action, "start_time", event, steps)
    if start_time:
        payload["type"] = 5
        payload["start_time"] = _parse_start_time(start_time)
        duration_raw = _rendered(action, "duration", event, steps) or "60"
    else:
        payload["type"] = 6
        duration_raw = _rendered(action, "duration", event, steps)
    if duration_raw:
        try:
            duration = int(duration_raw)
        except ValueError:
            raise ValueError("duration must be a whole number of minutes") from None
        if duration <= 0:
            raise ValueError("duration must be a positive number of minutes")
        payload["duration"] = duration
    for key in ("timezone", "agenda"):
        value = _rendered(action, key, event, steps)
        if value:
            payload[key] = value
    # Rendered like any merge field; literal JSON braces are written {{…}}.
    settings_raw = _rendered(action, "settings", event, steps)
    if settings_raw:
        try:
            settings = json.loads(settings_raw)
        except ValueError:
            raise ValueError("settings must be a JSON object") from None
        if not isinstance(settings, dict):
            raise ValueError("settings must be a JSON object")
        payload["settings"] = settings
    return payload


def _create_webinar(access_token, payload, *, transport=None):
    """One POST /users/me/webinars; the created webinar's API object."""
    status, data = _request_json("POST", access_token, "/users/me/webinars",
                                 payload, transport=transport)
    if status >= 300:
        _raise_zoom_error("zoom create webinar", status, data)
    return data


def run_zoom_create_webinar(action, event, *, transport=None, steps=None):
    """Create a webinar on the connected user's account (Zapier's Create
    Webinar).

    A rendered ``start_time`` schedules the webinar (Zoom type 5 — the
    common "schedule a session from a form/email" chain); without one the
    webinar is recurring with no fixed time (Zoom type 6). ``settings``
    takes a JSON object of Zoom webinar settings verbatim. Output:
    ``{created: true, scheduled, webinar}`` with the webinar's id, topic,
    start_time, duration, join/start URLs and passcode.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    payload = _create_webinar_payload(action, event, steps)
    data = _create_webinar(access_token, payload, transport=transport)
    return {"created": True, "scheduled": payload.get("type") == 5,
            "webinar": _created_webinar_view(data)}


def run_zoom_update_webinar(action, event, *, transport=None, steps=None):
    """Update one webinar's schedule or metadata (PATCH /webinars/{id}).

    Only the fields that render non-empty are sent — Zoom patches those and
    leaves the rest of the webinar untouched, so a workflow can move one
    session's start time without restating its agenda. The field rules are
    zoom_update_meeting's: a rendered ``start_time`` must parse as ISO 8601,
    ``duration`` is a positive whole number of minutes, ``settings`` is a
    JSON object. Updating nothing at all is a clear error, not an empty
    PATCH. Zoom answers 204 with no body, so the output carries the webinar
    id and what was sent: ``{updated: true, webinar_id, updated_fields}``.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    webinar_id = _rendered(action, "webinar_id", event, steps)
    if not webinar_id:
        raise ValueError("zoom_update_webinar requires webinar_id")
    payload = {}
    topic = _rendered(action, "topic", event, steps)
    if topic:
        payload["topic"] = topic
    start_time = _rendered(action, "start_time", event, steps)
    if start_time:
        payload["start_time"] = _parse_start_time(start_time)
    duration_raw = _rendered(action, "duration", event, steps)
    if duration_raw:
        try:
            duration = int(duration_raw)
        except ValueError:
            raise ValueError("duration must be a whole number of minutes") from None
        if duration <= 0:
            raise ValueError("duration must be a positive number of minutes")
        payload["duration"] = duration
    for key in ("timezone", "agenda"):
        value = _rendered(action, key, event, steps)
        if value:
            payload[key] = value
    # Rendered like any merge field; literal JSON braces are written {{…}}.
    settings_raw = _rendered(action, "settings", event, steps)
    if settings_raw:
        try:
            settings = json.loads(settings_raw)
        except ValueError:
            raise ValueError("settings must be a JSON object") from None
        if not isinstance(settings, dict):
            raise ValueError("settings must be a JSON object")
        payload["settings"] = settings
    if not payload:
        raise ValueError(
            "zoom_update_webinar needs at least one of: topic, start_time, "
            "duration, timezone, agenda, settings")
    status, data = _request_json(
        "PATCH", access_token,
        f"/webinars/{urllib.parse.quote(webinar_id, safe='')}",
        payload, transport=transport)
    if status >= 300:
        _raise_zoom_error("zoom update webinar", status, data)
    return {"updated": True, "webinar_id": str(webinar_id),
            "updated_fields": sorted(payload)}


def run_zoom_find_webinar(action, event, *, transport=None, steps=None):
    """Find a Zoom webinar by id, or by topic across its ``scope`` window
    (Zapier's Find Webinar).

    The zoom_find_meeting shape, one level up: the id path hits
    ``/webinars/{id}`` (a dead id is the ``found: False`` verdict, not an
    error), the topic path filters the ``/users/me/webinars`` listing —
    upcoming by default, past with ``scope: past`` — by ``match`` mode.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    webinar_id = render(str(action.get("webinar_id") or ""), event, steps).strip()
    if webinar_id:
        status, data = _get_json(
            access_token,
            f"/webinars/{urllib.parse.quote(webinar_id, safe='')}",
            transport=transport,
        )
        if status == 404:
            return {"found": False, "webinar": None}
        if status >= 300:
            _raise_zoom_error("zoom webinar lookup", status, data)
        return {"found": True, "webinar": _webinar_view(data)}
    topic = render(str(action.get("topic") or ""), event, steps).strip()
    if not topic:
        raise ValueError("zoom_find_webinar needs webinar_id or topic")
    mode = str(action.get("match") or "contains").strip().lower()
    if mode not in MATCH_MODES:
        raise ValueError("match must be one of: contains, exact")
    scope = str(action.get("scope") or "upcoming").strip().lower()
    if scope not in FIND_SCOPES:
        raise ValueError("scope must be one of: upcoming, past")
    listing_type = "previous_meetings" if scope == "past" else "upcoming"
    status, data = _get_json(
        access_token,
        "/users/me/webinars?" + urllib.parse.urlencode(
            {"type": listing_type, "per_page": 300}),
        transport=transport,
    )
    if status >= 300:
        _raise_zoom_error("zoom webinar list", status, data)
    for webinar in data.get("webinars") or []:
        if isinstance(webinar, dict) and _topic_matches(webinar.get("topic"),
                                                        topic.lower(), mode):
            return {"found": True, "webinar": _webinar_view(webinar),
                    "matched_by": "topic", "scope": scope}
    return {"found": False, "webinar": None, "matched_by": "topic", "scope": scope}


def run_zoom_add_webinar_registrant(action, event, *, transport=None, steps=None):
    """Register one attendee for a webinar (POST /webinars/{id}/registrants).

    The zoom_add_registrant shape for webinars: the response's ``join_url``
    is the registrant's personalized link — unique per registrant, the thing
    an invite email templates. The webinar must have registration enabled
    (Zoom answers HTTP 4xx otherwise, surfacing its message). Output:
    ``{registered: true, webinar_id, registrant_id, join_url}``.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    webinar_id = _rendered(action, "webinar_id", event, steps)
    if not webinar_id:
        raise ValueError("zoom_add_webinar_registrant requires webinar_id")
    email = _rendered(action, "email", event, steps)
    if not email:
        raise ValueError("zoom_add_webinar_registrant requires email")
    payload = {"email": email}
    for key in ("first_name", "last_name"):
        value = _rendered(action, key, event, steps)
        if value:
            payload[key] = value
    status, data = _request_json(
        "POST", access_token,
        f"/webinars/{urllib.parse.quote(webinar_id, safe='')}/registrants",
        payload, transport=transport)
    if status >= 300:
        _raise_zoom_error("zoom add webinar registrant", status, data)
    return {
        "registered": True,
        "webinar_id": str(webinar_id),
        "registrant_id": str(data.get("registrant_id") or ""),
        "join_url": data.get("join_url"),
    }


def run_zoom_delete_webinar(action, event, *, transport=None, steps=None):
    """Delete one webinar (DELETE /webinars/{id}).

    The zoom_delete_meeting shape one level up: ``occurrence_id`` scopes the
    delete to one occurrence of a recurring webinar — without it the whole
    series goes away. Zoom answers 204 with no body; a webinar id that is
    already gone is Zoom's 404, an error like any provider refusal — branch
    on zoom_find_webinar's ``found`` verdict first when the webinar may not
    exist. Output: ``{deleted: true, webinar_id}`` (plus ``occurrence_id``
    when one was sent).
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    webinar_id = _rendered(action, "webinar_id", event, steps)
    if not webinar_id:
        raise ValueError("zoom_delete_webinar requires webinar_id")
    path = f"/webinars/{urllib.parse.quote(webinar_id, safe='')}"
    occurrence_id = _rendered(action, "occurrence_id", event, steps)
    if occurrence_id:
        path += "?" + urllib.parse.urlencode({"occurrence_id": occurrence_id})
    status, data = _request_json("DELETE", access_token, path, transport=transport)
    if status >= 300:
        _raise_zoom_error("zoom delete webinar", status, data)
    output = {"deleted": True, "webinar_id": str(webinar_id)}
    if occurrence_id:
        output["occurrence_id"] = occurrence_id
    return output


def run_zoom_list_past_webinar_participants(action, event, *, transport=None,
                                            steps=None):
    """List who attended one past webinar (GET
    /past_webinars/{id}/participants).

    The zoom_list_past_participants shape one level up — pair with the
    webinar.ended trigger and template the attendees (name, email,
    join/leave times, duration) into an attendance email or a sheet row.
    Pagination follows Zoom's ``next_page_token`` and stops at
    PARTICIPANTS_MAX_PAGES pages or PARTICIPANTS_CAP participants,
    whichever comes first, to keep the run inside the lambda budget;
    ``count`` is what was fetched. A dead webinar id is Zoom's 404 — an
    error like any provider refusal; branch on zoom_find_webinar's
    ``found`` verdict first when the webinar may not exist. Output:
    ``{participants, count, webinar_id}``.
    """
    connection = _zoom_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    webinar_id = _rendered(action, "webinar_id", event, steps)
    if not webinar_id:
        raise ValueError(
            "zoom_list_past_webinar_participants requires webinar_id")
    participants = []
    params = {"per_page": PARTICIPANTS_PAGE_SIZE}
    for _page in range(PARTICIPANTS_MAX_PAGES):
        path = (f"/past_webinars/{urllib.parse.quote(webinar_id, safe='')}"
                "/participants?" + urllib.parse.urlencode(params))
        status, data = _get_json(access_token, path, transport=transport)
        if status >= 300:
            _raise_zoom_error("zoom webinar participant list", status, data)
        for entry in data.get("participants") or []:
            if isinstance(entry, dict):
                participants.append(_participant_view(entry))
        next_page = data.get("next_page_token")
        if not next_page or len(participants) >= PARTICIPANTS_CAP:
            break
        params = {**params, "next_page_token": next_page}
    return {"participants": participants, "count": len(participants),
            "webinar_id": str(webinar_id)}
