"""Read-only production course metadata for workflow fact checks."""
import json
import re
import urllib.request
from datetime import datetime, timezone
from src.dapier.connections import credentials, records
from src.dapier.engine.actions import base
from src.dapier.engine.actions.templating import render

ROOT = "https://courses.datatalks.club/api/courses/"

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _transport(method, url, *, headers, body, timeout):
    request = urllib.request.Request(url, headers=headers, method=method)
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        return response.status, response.read()

def _items_by_slug(data, key, fields):
    items = data.get(key)
    if not isinstance(items, list):
        raise ValueError()
    result = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError()
        slug = item.get("slug")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in result:
            raise ValueError()
        result[slug] = {field: item.get(field) for field in fields}
    return result

def run_read_course(action, event, *, steps=None, transport=None):
    slug = render(action.get("course_slug", ""), event, steps)
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ValueError("cmp_read_course needs a valid course slug")
    connection = base._connected_connection(action["connection_id"])
    if connection.get("provider") != "cmp":
        raise ValueError("cmp_read_course needs a CMP connection")
    records.check_binding(connection, "courses.datatalks.club")
    token = credentials.get_credential(connection["credential_id"]).get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,512}", token):
        raise ValueError("CMP connection has no usable stored token")
    timeout = action.get("timeout_seconds", 15)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 30:
        raise ValueError("cmp_read_course timeout_seconds must be 1-30")
    try:
        status, raw = (transport or _transport)("GET", ROOT + slug + "/",
            headers={"authorization": "Token " + token}, body=None, timeout=timeout)
        data = json.loads(raw) if status == 200 else {}
        if not isinstance(data, dict) or data.get("slug") != slug:
            raise ValueError()
        return {
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "course": {key: data.get(key) for key in ("slug", "title", "start_date", "end_date", "finished", "visible")},
            "homeworks": _items_by_slug(data, "homeworks", ("slug", "title", "due_date", "state")),
            "projects": _items_by_slug(data, "projects", ("slug", "title", "submission_due_date", "peer_review_due_date", "state")),
        }
    except Exception:
        raise ValueError("CMP course read failed or returned unexpected data") from None
