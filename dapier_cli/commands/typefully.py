"""Implementation of the `dapier typefully` commands."""
import http.client
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .. import api
from .shared import print_json

__all__ = [
    "typefully_social_sets", "typefully_drafts", "typefully_draft",
    "typefully_queue", "typefully_create_draft", "typefully_patch_draft",
    "typefully_upload_media", "typefully_media_status",
]

DEFAULT_SOCIAL_SET_ID = 182343
MAX_MEDIA_BYTES = 100 * 1024 * 1024
MEDIA_UPLOAD_TIMEOUT_SECONDS = 300
MEDIA_POLL_ATTEMPTS = 90
MEDIA_POLL_INTERVAL_SECONDS = 2.0


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
    print_json(data)
    return 0


def typefully_draft(api_url, draft_id, social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False):
    path = f"/api/agent/typefully/social-sets/{int(social_set_id)}/drafts/{int(draft_id)}"
    data = api.call(api_url, "GET", path, debug=debug)
    print_json(data)
    return 0


def typefully_queue(api_url, start_date, end_date, social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False):
    from urllib.parse import urlencode
    path = (f"/api/agent/typefully/social-sets/{int(social_set_id)}/queue?"
            + urlencode({"start_date": start_date, "end_date": end_date}))
    data = api.call(api_url, "GET", path, debug=debug)
    print_json(data)
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
    print_json(data)
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
    print_json(data)
    return 0


def _put_presigned_file(url, file_path, size, timeout=MEDIA_UPLOAD_TIMEOUT_SECONDS, debug=False):
    """Stream file bytes directly to a presigned HTTPS URL with only Content-Length."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ValueError("direct media upload failed")
    parsed = urlparse(url)
    if not parsed.hostname:
        raise ValueError("direct media upload failed")
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if debug:
        print(f"PUT https://{parsed.hostname}{parsed.path or '/'} ({size} bytes)")
    try:
        with Path(file_path).open("rb") as handle:
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
            try:
                conn.request("PUT", path, body=handle, headers={"Content-Length": str(size)})
                response = conn.getresponse()
                status = response.status
                response.read()
            finally:
                conn.close()
    except Exception:
        raise ValueError("direct media upload failed") from None
    if status not in (200, 201, 204):
        raise ValueError(f"direct media upload failed (HTTP {status})")
    return status


def typefully_upload_media(api_url, file, alt_text=None, file_name=None,
                           social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False, sleep=None):
    path = Path(file)
    try:
        if not path.is_file():
            raise OSError("not a regular file")
        size = path.stat().st_size
        with path.open("rb"):
            pass
    except OSError:
        print(f"Error: cannot read {file}", file=sys.stderr)
        return 2
    if size == 0:
        print("Error: media file is empty", file=sys.stderr)
        return 2
    if size > MAX_MEDIA_BYTES:
        print("Error: media file exceeds 100 MB max size", file=sys.stderr)
        return 2
    name = file_name or path.name
    body = {"file_name": name, "size_bytes": size}
    if alt_text is not None:
        body["alt_text"] = alt_text
    endpoint = f"/api/agent/typefully/social-sets/{int(social_set_id)}/media/upload"
    upload_info = api.call(api_url, "POST", endpoint, body=body, debug=debug)
    media_id = upload_info.get("media_id")
    upload_url = upload_info.get("upload_url")
    if not isinstance(media_id, str) or not media_id or not isinstance(upload_url, str) or not upload_url:
        print("Error: invalid media upload response from server", file=sys.stderr)
        return 2
    try:
        _put_presigned_file(upload_url, path, size, debug=debug)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    pause = sleep or time.sleep
    status_path = f"/api/agent/typefully/social-sets/{int(social_set_id)}/media/{media_id}"
    final = {}
    for attempt in range(MEDIA_POLL_ATTEMPTS):
        final = api.call(api_url, "GET", status_path, debug=debug)
        if final.get("status") in ("ready", "failed"):
            break
        if attempt + 1 < MEDIA_POLL_ATTEMPTS:
            pause(MEDIA_POLL_INTERVAL_SECONDS)
    # Print identifiers only: the signed upload URL and file bytes must never reach stdout.
    safe = {"media_id": media_id, "file_name": upload_info.get("file_name") or name}
    for key in ("status", "error_reason"):
        if key in final and final[key] is not None:
            safe[key] = final[key]
    safe.setdefault("status", "unknown")
    print_json(safe)
    return 0


def typefully_media_status(api_url, media_id, social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False):
    if not str(media_id or "").strip():
        print("Error: media_id is required", file=sys.stderr)
        return 2
    path = f"/api/agent/typefully/social-sets/{int(social_set_id)}/media/{str(media_id).strip()}"
    data = api.call(api_url, "GET", path, debug=debug)
    print_json(data)
    return 0
