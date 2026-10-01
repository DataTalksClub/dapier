"""zoom_find_recording / zoom_delete_recording: look up and remove one
account's cloud recordings."""
import urllib.parse
from datetime import datetime, timedelta, timezone

from ....connections import tokens
from ..templating import render
from .core import (DELETE_RECORDING_ACTIONS, MATCH_MODES, _get_json, _pkg,
                   _raise_zoom_error, _recording_view, _rendered,
                   _request_json, _topic_matches)


def _connection(connection_id):
    return _pkg("_zoom_connection")(connection_id)


def run_zoom_find_recording(action, event, *, transport=None, steps=None):
    """Find Zoom cloud recordings: by meeting id, by topic, or the latest.

    The id path hits the meeting's own recordings endpoint (Zoom's 404 for
    a dead id — the listing only covers the last 30 days — is a verdict,
    ``found: False``, not an error). Without an id the 30-day listing is
    consulted, filtered by ``topic`` (``match`` contains/exact) when given,
    otherwise left whole so the most recent recording tops the list — the
    "share my latest recording" chain needs no arguments at all."""
    connection = _connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = render(str(action.get("meeting_id") or ""), event, steps).strip()
    if meeting_id:
        return _find_recording_by_id(access_token, meeting_id, transport=transport)
    return _find_recording_by_topic(action, event, steps, access_token,
                                    transport=transport)


def _find_recording_by_id(access_token, meeting_id, *, transport=None):
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


def _find_recording_by_topic(action, event, steps, access_token, *, transport=None):
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
    connection = _connection(action["connection_id"])
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
