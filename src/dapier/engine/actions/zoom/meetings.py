"""zoom_find_meeting / zoom_update_meeting / zoom_delete_meeting /
zoom_list_past_participants: the meeting lifecycle runners."""
import urllib.parse

from ....connections import tokens
from ..templating import render
from .core import (_collect_participants, _created_view, _flag, _get_json,
                   _meeting_view, _pkg, _raise_zoom_error, _rendered,
                   _request_json, _topic_matches, _topic_query,
                   _update_fields)
from .create import _create_meeting, _create_meeting_payload


def _connection(connection_id):
    return _pkg("_zoom_connection")(connection_id)


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
    connection = _connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = render(str(action.get("meeting_id") or ""), event, steps).strip()
    if meeting_id:
        return _find_meeting_by_id(access_token, meeting_id, transport=transport)
    return _find_meeting_by_topic(action, event, steps, access_token,
                                  transport=transport)


def _find_meeting_by_id(access_token, meeting_id, *, transport=None):
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


def _find_meeting_by_topic(action, event, steps, access_token, *, transport=None):
    topic, mode, scope = _topic_query(
        action, event, steps,
        needs="zoom_find_meeting needs meeting_id or topic")
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
    return _create_missing_meeting(action, event, steps, access_token,
                                   transport=transport)


def _create_missing_meeting(action, event, steps, access_token, *, transport=None):
    payload = _create_meeting_payload(action, event, steps,
                                      what="zoom_find_meeting create_if_missing")
    created = _create_meeting(access_token, payload, transport=transport)
    return {"found": False, "created": True,
            "scheduled": payload.get("type") == 2,
            "meeting": _created_view(created)}


# --- zoom_update_meeting: reschedule one, patch only what rendered ------------


def run_zoom_update_meeting(action, event, *, transport=None, steps=None):
    """Update one meeting's schedule or metadata (PATCH /meetings/{id}).

    Only the fields that render non-empty are sent — Zoom patches those and
    leaves the rest of the meeting untouched, so a workflow can move one
    occurrence's start time without restating its agenda. The field rules
    are create_meeting's: a rendered ``start_time`` must parse as ISO 8601,
    ``duration`` is a positive whole number of minutes, ``settings`` is a
    JSON object. Updating nothing at all is a clear error, not an empty
    PATCH. Zoom answers 204 with no body, so the output carries the meeting
    id and what was sent: ``{updated: true, meeting_id, updated_fields}``."""
    connection = _connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = _rendered(action, "meeting_id", event, steps)
    if not meeting_id:
        raise ValueError("zoom_update_meeting requires meeting_id")
    payload = _update_fields(action, event, steps)
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
    connection = _connection(action["connection_id"])
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


# --- zoom_list_past_participants: who attended one past meeting ----------------


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
    exist. Output: ``{participants, count, meeting_id}``."""
    connection = _connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    meeting_id = _rendered(action, "meeting_id", event, steps)
    if not meeting_id:
        raise ValueError("zoom_list_past_participants requires meeting_id")
    participants = _collect_participants(
        access_token, "meetings", meeting_id,
        what="zoom participant list", transport=transport)
    return {"participants": participants, "count": len(participants),
            "meeting_id": str(meeting_id)}
