"""Read Zoom transcript metadata and AI Companion summaries."""
from urllib.parse import quote, urlencode

from src.dapier.connections import tokens
from .core import _get_json, _pkg, _raise_zoom_error, _rendered


def run_zoom_meeting_artifact(action, event, *, endpoint, steps=None, transport=None):
    meeting_id = _rendered(action, "meeting_id", event, steps)
    if not meeting_id:
        raise ValueError("Zoom transcript and summary lookups require meeting_id")
    encoded = quote(meeting_id, safe="")
    # The transcript endpoint documents double encoding for any slash.
    if meeting_id.startswith("/") or "//" in meeting_id or (
        endpoint == "transcript" and "/" in meeting_id
    ):
        encoded = quote(encoded, safe="")
    return _read(action, f"/meetings/{encoded}/{endpoint}", transport=transport)


def run_zoom_list_meeting_summaries(action, event, *, steps=None, transport=None):
    path = "/users/me/meeting_summaries"
    next_page = _rendered(action, "next_page_token", event, steps)
    if next_page:
        path += "?" + urlencode({"next_page_token": next_page})
    return _read(action, path, transport=transport)


def _read(action, path, *, transport):
    connection = _pkg("_zoom_connection")(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    status, data = _get_json(access_token, path, transport=transport)
    if status >= 300:
        _raise_zoom_error("Zoom meeting artifact request", status, data)
    return data
