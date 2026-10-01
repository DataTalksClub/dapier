"""zoom_add_registrant / zoom_add_webinar_registrant: seat one attendee
on a meeting or webinar that requires registration."""
import urllib.parse

from ....connections import tokens
from .core import _pkg, _raise_zoom_error, _rendered, _request_json


def _connection(connection_id):
    return _pkg("_zoom_connection")(connection_id)


def run_zoom_add_registrant(action, event, *, transport=None, steps=None):
    """Register one attendee for a meeting that requires registration
    (POST /meetings/{id}/registrants).

    The response's ``join_url`` is the registrant's personalized link — the
    thing an invite email templates — unique per registrant, unlike the
    meeting's public join URL. The meeting must have registration enabled
    (Zoom answers HTTP 4xx otherwise, surfacing its message). Output:
    ``{registered: true, meeting_id, registrant_id, join_url}``.
    """
    connection = _connection(action["connection_id"])
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


def run_zoom_add_webinar_registrant(action, event, *, transport=None, steps=None):
    """Register one attendee for a webinar (POST /webinars/{id}/registrants).

    The zoom_add_registrant shape for webinars: the response's ``join_url``
    is the registrant's personalized link — unique per registrant, the thing
    an invite email templates. The webinar must have registration enabled
    (Zoom answers HTTP 4xx otherwise, surfacing its message). Output:
    ``{registered: true, webinar_id, registrant_id, join_url}``.
    """
    connection = _connection(action["connection_id"])
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
