"""zoom_find_webinar / zoom_update_webinar / zoom_delete_webinar /
zoom_list_past_webinar_participants: the webinar runners — the meeting
shapes one level up on Zoom's REST API (webinar ids are NOT meeting ids:
a registrant added to the wrong kind of id is Zoom's 4xx)."""
import urllib.parse

from src.dapier.connections import tokens
from src.dapier.engine.actions.templating import render
from .core import (_collect_participants, _get_json, _pkg,
                   _raise_zoom_error, _rendered, _request_json,
                   _topic_matches, _topic_query, _update_fields,
                   _webinar_view)


def _connection(connection_id):
    return _pkg("_zoom_connection")(connection_id)


def run_zoom_find_webinar(action, event, *, transport=None, steps=None):
    """Find a Zoom webinar by id, or by topic across its ``scope`` window
    (Zapier's Find Webinar).

    The zoom_find_meeting shape, one level up: the id path hits
    ``/webinars/{id}`` (a dead id is the ``found: False`` verdict, not an
    error), the topic path filters the ``/users/me/webinars`` listing —
    upcoming by default, past with ``scope: past`` — by ``match`` mode."""
    connection = _connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    webinar_id = render(str(action.get("webinar_id") or ""), event, steps).strip()
    if webinar_id:
        return _find_webinar_by_id(access_token, webinar_id, transport=transport)
    return _find_webinar_by_topic(action, event, steps, access_token,
                                  transport=transport)


def _find_webinar_by_id(access_token, webinar_id, *, transport=None):
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


def _find_webinar_by_topic(action, event, steps, access_token, *, transport=None):
    topic, mode, scope = _topic_query(
        action, event, steps,
        needs="zoom_find_meeting needs webinar_id or topic")
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


def run_zoom_update_webinar(action, event, *, transport=None, steps=None):
    """Update one webinar's schedule or metadata (PATCH /webinars/{id}).

    Only the fields that render non-empty are sent — Zoom patches those and
    leaves the rest of the webinar untouched, so a workflow can move one
    session's start time without restating its agenda. The field rules are
    zoom_update_meeting's: a rendered ``start_time`` must parse as ISO 8601,
    ``duration`` is a positive whole number of minutes, ``settings`` is a
    JSON object. Updating nothing at all is a clear error, not an empty
    PATCH. Zoom answers 204 with no body, so the output carries the webinar
    id and what was sent: ``{updated: true, webinar_id, updated_fields}``."""
    connection = _connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    webinar_id = _rendered(action, "webinar_id", event, steps)
    if not webinar_id:
        raise ValueError("zoom_update_webinar requires webinar_id")
    payload = _update_fields(action, event, steps)
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
    connection = _connection(action["connection_id"])
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
    ``{participants, count, webinar_id}``."""
    connection = _connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    webinar_id = _rendered(action, "webinar_id", event, steps)
    if not webinar_id:
        raise ValueError(
            "zoom_list_past_webinar_participants requires webinar_id")
    participants = _collect_participants(
        access_token, "webinars", webinar_id,
        what="zoom webinar participant list", transport=transport)
    return {"participants": participants, "count": len(participants),
            "webinar_id": str(webinar_id)}
