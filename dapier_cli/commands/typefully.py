"""Implementation of the `dapier typefully` commands."""
import base64
import json
import sys
from pathlib import Path

from .. import api

__all__ = [
    "typefully_social_sets", "typefully_drafts", "typefully_draft",
    "typefully_queue", "typefully_create_draft", "typefully_patch_draft",
    "typefully_upload_media", "typefully_media_status",
]

DEFAULT_SOCIAL_SET_ID = 182343


def _print_json(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def typefully_social_sets(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/typefully/social-sets", debug=debug)
    items = data.get("items") or []
    if not items:
        print("No social sets.")
        return 0
    print(f"{'ID':<12} {'NAME':<28} USERNAME")
    for item in items:
        print(f"{str(item.get('id') or ''):<12} {str(item.get('name') or '')[:28]:<28} {item.get('username') or ''}")
    return 0


def typefully_drafts(api_url, social_set_id=DEFAULT_SOCIAL_SET_ID, status=None, debug=False):
    from urllib.parse import urlencode
    params = {"social_set_id": int(social_set_id)}
    if status:
        params["status"] = status
    path = "/api/agent/typefully/drafts?" + urlencode(params)
    data = api.call(api_url, "GET", path, debug=debug)
    _print_json(data)
    return 0


def typefully_draft(api_url, draft_id, social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False):
    path = f"/api/agent/typefully/social-sets/{int(social_set_id)}/drafts/{int(draft_id)}"
    data = api.call(api_url, "GET", path, debug=debug)
    _print_json(data)
    return 0


def typefully_queue(api_url, start_date, end_date, social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False):
    from urllib.parse import urlencode
    path = (f"/api/agent/typefully/social-sets/{int(social_set_id)}/queue?"
            + urlencode({"start_date": start_date, "end_date": end_date}))
    data = api.call(api_url, "GET", path, debug=debug)
    _print_json(data)
    return 0


def typefully_create_draft(api_url, path, social_set_id=DEFAULT_SOCIAL_SET_ID,
                           allow_schedule=False, debug=False):
    if path == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            print(f"Error: cannot read {path}", file=sys.stderr)
            return 2
    try:
        body = json.loads(raw)
    except ValueError:
        print("Error: create-draft body must be JSON", file=sys.stderr)
        return 2
    if not isinstance(body, dict):
        print("Error: create-draft body must be a JSON object", file=sys.stderr)
        return 2
    endpoint = f"/api/agent/typefully/social-sets/{int(social_set_id)}/drafts"
    if allow_schedule:
        endpoint += "?allow_schedule=true"
    else:
        for forbidden in ("publish_at", "plan_at"):
            if body.get(forbidden) is not None:
                print(f"Error: {forbidden} requires explicit approval; omit it for an unscheduled draft"
                      " (or pass --allow-schedule)", file=sys.stderr)
                return 2
    data = api.call(api_url, "POST", endpoint, body=body, debug=debug)
    _print_json(data)
    return 0


def _read_patch_body(path):
    if path == "-":
        raw = sys.stdin.read()
    else:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except OSError:
            return None, f"Error: cannot read {path}"
    try:
        body = json.loads(raw)
    except ValueError:
        return None, "Error: patch-draft body must be JSON"
    if not isinstance(body, dict) or not body:
        return None, "Error: patch-draft body must be a non-empty JSON object"
    return body, None


def typefully_patch_draft(api_url, draft_id, path, social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False):
    try:
        draft_id = int(draft_id)
    except (TypeError, ValueError):
        print("Error: draft_id must be a positive integer", file=sys.stderr)
        return 2
    if draft_id < 1:
        print("Error: draft_id must be a positive integer", file=sys.stderr)
        return 2
    body, error = _read_patch_body(path)
    if error:
        print(error, file=sys.stderr)
        return 2
    endpoint = f"/api/agent/typefully/social-sets/{int(social_set_id)}/drafts/{draft_id}"
    data = api.call(api_url, "PATCH", endpoint, body=body, debug=debug)
    _print_json(data)
    return 0


def typefully_upload_media(api_url, file, alt_text=None, file_name=None,
                           social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False):
    try:
        content = Path(file).read_bytes()
    except OSError:
        print(f"Error: cannot read {file}", file=sys.stderr)
        return 2
    if not content:
        print("Error: media file is empty", file=sys.stderr)
        return 2
    name = file_name or Path(file).name
    body = {"file_name": name, "content_base64": base64.b64encode(content).decode("ascii")}
    if alt_text is not None:
        body["alt_text"] = alt_text
    endpoint = f"/api/agent/typefully/social-sets/{int(social_set_id)}/media"
    data = api.call(api_url, "POST", endpoint, body=body, debug=debug)
    # Print identifiers only: the signed upload URL never leaves the server,
    # and file bytes must never reach stdout.
    safe = {key: data[key] for key in ("media_id", "file_name", "status", "error_reason") if key in data}
    _print_json(safe if safe else {"status": "unknown"})
    return 0


def typefully_media_status(api_url, media_id, social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False):
    if not str(media_id or "").strip():
        print("Error: media_id is required", file=sys.stderr)
        return 2
    path = f"/api/agent/typefully/social-sets/{int(social_set_id)}/media/{str(media_id).strip()}"
    data = api.call(api_url, "GET", path, debug=debug)
    _print_json(data)
    return 0
