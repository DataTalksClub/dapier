"""Read Zoom transcript metadata and AI Companion summaries."""
from urllib.parse import quote, urlencode

from src.dapier.connections import tokens
from .core import _get_json, _pkg, _raise_zoom_error, _rendered


def run_zoom_meeting_artifact(action, event, *, endpoint, steps=None, transport=None):
    meeting_id = _rendered(action, "meeting_id", event, steps)
    if not meeting_id:
        raise ValueError("Zoom transcript and summary lookups require meeting_id")
    access_token = _access_token(action, transport=transport)
    if meeting_id.isascii() and meeting_id.isdecimal():
        meeting_id = _past_meeting_uuid(access_token, meeting_id, transport=transport)
    encoded = quote(meeting_id, safe="")
    # The transcript endpoint documents double encoding for any slash.
    if meeting_id.startswith("/") or "//" in meeting_id or (
        endpoint == "transcript" and "/" in meeting_id
    ):
        encoded = quote(encoded, safe="")
    return _read(access_token, f"/meetings/{encoded}/{endpoint}",
                 what=f"Zoom {endpoint} request", transport=transport)


def _past_meeting_uuid(access_token, meeting_id, *, transport):
    path = f"/past_meetings/{meeting_id}"
    status, data = _get_json(access_token, f"{path}/instances", transport=transport)
    if status >= 300 and status != 404:
        _raise_zoom_error("Zoom past meeting instances request", status, data)
    if status < 300:
        instances = (
            entry for entry in data.get("meetings") or []
            if isinstance(entry, dict) and entry.get("uuid")
        )
        latest = max(instances, key=lambda entry: entry.get("start_time") or "", default=None)
        if latest:
            return latest["uuid"]

    details = _read(access_token, path, what="Zoom past meeting details request",
                    transport=transport)
    uuid = details.get("uuid")
    if not uuid:
        raise RuntimeError(f"Zoom returned no past-instance UUID for meeting {meeting_id}")
    return uuid


def run_zoom_list_meeting_summaries(action, event, *, steps=None, transport=None):
    path = "/users/me/meeting_summaries"
    next_page = _rendered(action, "next_page_token", event, steps)
    if next_page:
        path += "?" + urlencode({"next_page_token": next_page})
    access_token = _access_token(action, transport=transport)
    return _read(access_token, path, what="Zoom meeting summaries request", transport=transport)


def _access_token(action, *, transport):
    connection = _pkg("_zoom_connection")(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    return access_token


def _read(access_token, path, *, what, transport):
    status, data = _get_json(access_token, path, transport=transport)
    if status >= 300:
        _raise_zoom_error(what, status, data)
    return data
