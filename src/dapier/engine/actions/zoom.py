"""zoom_find_meeting / zoom_find_recording: look up Zoom meetings and cloud
recordings through an OAuth connection."""
import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from ...connections import tokens
from . import base
from .templating import render

API_URL = "https://api.zoom.us/v2"
MATCH_MODES = ("contains", "exact")


def _zoom_connection(connection_id):
    return base._connected_connection(connection_id)


def _get_json(access_token, path, *, transport=None):
    """One Zoom GET; returns ``(status, parsed dict)`` without raising on status."""
    transport = transport or base._default_transport
    url = f"{API_URL}{path}"
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport("GET", url, headers=headers, body=None, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"zoom unreachable: {type(exc).__name__}")
    try:
        data = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        data = {}
    return status, data if isinstance(data, dict) else {}


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
    """Find a Zoom meeting by id, or by topic across upcoming meetings.

    A missing meeting is a verdict (``found: False``), not an error — Zoom's
    404 for a dead meeting id and an unmatched topic both land there — so a
    workflow can branch on the outcome.
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
    status, data = _get_json(
        access_token,
        "/users/me/meetings?" + urllib.parse.urlencode({"type": "upcoming", "per_page": 300}),
        transport=transport,
    )
    if status >= 300:
        _raise_zoom_error("zoom meeting list", status, data)
    for meeting in data.get("meetings") or []:
        if isinstance(meeting, dict) and _topic_matches(meeting.get("topic"), topic.lower(), mode):
            return {"found": True, "meeting": _meeting_view(meeting), "matched_by": "topic"}
    return {"found": False, "meeting": None, "matched_by": "topic"}


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
