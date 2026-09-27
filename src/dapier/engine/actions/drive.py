"""drive_find_file action: find a Drive file through a Google connection.

Drive's first workflow action. It searches the connection's Drive
``files`` listing (the same resource discovery serves) and returns the most
recently modified match; a miss is a ``found: False`` output, not an error.
Downloads still ride the S3 upload's ``source_url`` with the connection's
bearer token — pair the two to fetch the file this action finds.
"""
import json
import urllib.parse

from ...connections import tokens
from . import base
from .templating import render

DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
FIND_MATCH_MODES = ("contains", "exact")
SEARCH_TIMEOUT = 15


def _drive_query(name, mode):
    """The Drive ``q`` filter: not trashed, name matched per mode."""
    escaped = name.replace("'", "\\'")
    operator = "=" if mode == "exact" else "contains"
    return f"trashed=false and name {operator} '{escaped}'"


def _search_files(access_token, query, *, transport=None):
    """One Drive ``files.list`` call, parsed; HTTP errors raise readable."""
    transport = transport or base._default_transport
    url = DRIVE_FILES_URL + "?" + urllib.parse.urlencode({
        "pageSize": "10",
        "orderBy": "modifiedTime desc",
        "fields": "files(id,name,mimeType,modifiedTime)",
        "supportsAllDrives": "true",
        "q": query,
    })
    headers = {"authorization": f"Bearer {access_token}"}
    try:
        status, response = transport("GET", url, headers=headers, body=None,
                                     timeout=SEARCH_TIMEOUT)
    except Exception as exc:
        raise RuntimeError(f"google drive unreachable: {type(exc).__name__}")
    if status >= 300:
        detail = ""
        try:
            error = json.loads(response.decode() or "{}").get("error")
            if isinstance(error, dict) and error.get("message"):
                detail = f" ({str(error['message'])[:200]})"
        except (ValueError, UnicodeDecodeError):
            pass
        raise RuntimeError(f"google drive search returned HTTP {status}{detail}")
    try:
        result = json.loads(response.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


def run_drive_find_file(action, event, *, transport=None, steps=None):
    """Find the most recently modified Drive file whose name matches.

    ``match`` selects ``contains`` (default) or ``exact`` against the file
    name. The output carries the first hit under ``file`` and how many
    candidates the search returned under ``count``.
    """
    connection = base._connected_connection(action["connection_id"])
    access_token, _info = tokens.get_access_token(connection, transport=transport)
    name = render(str(action.get("name") or ""), event, steps).strip()
    if not name:
        raise ValueError("drive_find_file requires a name")
    mode = str(action.get("match") or "contains").strip().lower()
    if mode not in FIND_MATCH_MODES:
        raise ValueError(f"drive_find_file match must be one of: {', '.join(FIND_MATCH_MODES)}")
    result = _search_files(access_token, _drive_query(name, mode), transport=transport)
    files = result.get("files") if isinstance(result.get("files"), list) else []
    if not files:
        return {"found": False, "file": None, "count": 0}
    first = files[0] if isinstance(files[0], dict) else {}
    return {
        "found": True,
        "file": {
            "id": first.get("id"),
            "name": first.get("name"),
            "mimeType": first.get("mimeType"),
            "modified": first.get("modifiedTime"),
        },
        "count": len(files),
    }
