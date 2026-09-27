"""zoom_find_meeting action: look up a Zoom meeting through an OAuth connection."""
import json
import urllib.parse

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
