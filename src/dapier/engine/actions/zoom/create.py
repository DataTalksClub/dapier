"""zoom_create_meeting / zoom_create_webinar: the create runners and the
payload builders zoom_find_meeting's create-if-missing branch shares."""
from ....connections import tokens
from .core import (_apply_duration, _apply_settings, _created_view,
                   _created_webinar_view, _parse_start_time, _pkg,
                   _raise_zoom_error, _rendered, _request_json)


def _connection(connection_id):
    return _pkg("_zoom_connection")(connection_id)


def _create_meeting_payload(action, event, steps, *, what="zoom_create_meeting"):
    """The POST /users/me/meetings payload from the shared create fields.

    A rendered ``start_time`` schedules the meeting (Zoom type 2 — the common
    "schedule a session from a form/email" chain); without one Zoom creates an
    instant meeting (type 1). ``settings`` takes a JSON object of Zoom meeting
    settings (join_before_host, waiting_room, …) verbatim. ``what`` names the
    action in error messages — zoom_find_meeting's create-if-missing branch
    shares this builder."""
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
    _apply_duration(payload, duration_raw)
    for key in ("timezone", "agenda"):
        value = _rendered(action, key, event, steps)
        if value:
            payload[key] = value
    _apply_settings(payload, _rendered(action, "settings", event, steps))
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
    connection = _connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    payload = _create_meeting_payload(action, event, steps)
    data = _create_meeting(access_token, payload, transport=transport)
    return {"created": True, "scheduled": payload.get("type") == 2,
            "meeting": _created_view(data)}


def _create_webinar_payload(action, event, steps, *, what="zoom_create_webinar"):
    """The POST /users/me/webinars payload from the shared create fields.

    Webinars have no instant kind (Zapier's is the same): a rendered
    ``start_time`` schedules a fixed webinar (Zoom type 5 — the common
    "schedule a session from a form/email" chain); without one the webinar
    is recurring with no fixed time (Zoom type 6). ``settings`` takes a JSON
    object of Zoom webinar settings verbatim."""
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
    _apply_duration(payload, duration_raw)
    for key in ("timezone", "agenda"):
        value = _rendered(action, key, event, steps)
        if value:
            payload[key] = value
    _apply_settings(payload, _rendered(action, "settings", event, steps))
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
    connection = _connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    payload = _create_webinar_payload(action, event, steps)
    data = _create_webinar(access_token, payload, transport=transport)
    return {"created": True, "scheduled": payload.get("type") == 5,
            "webinar": _created_webinar_view(data)}
