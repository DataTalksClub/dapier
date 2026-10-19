"""Implementation of the `dapier typefully` commands."""
import json
import sys
from pathlib import Path

from .. import api

__all__ = [
    "typefully_social_sets", "typefully_drafts", "typefully_draft",
    "typefully_queue", "typefully_create_draft",
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


def typefully_create_draft(api_url, path, social_set_id=DEFAULT_SOCIAL_SET_ID, debug=False):
    if path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).read_text(encoding="utf-8")
    try:
        body = json.loads(raw)
    except ValueError:
        print("Error: create-draft body must be JSON", file=sys.stderr)
        return 2
    if not isinstance(body, dict):
        print("Error: create-draft body must be a JSON object", file=sys.stderr)
        return 2
    for forbidden in ("publish_at", "plan_at"):
        if body.get(forbidden) is not None:
            print(f"Error: {forbidden} requires explicit approval; omit it for an unscheduled draft",
                  file=sys.stderr)
            return 2
    endpoint = f"/api/agent/typefully/social-sets/{int(social_set_id)}/drafts"
    data = api.call(api_url, "POST", endpoint, body=body, debug=debug)
    _print_json(data)
    return 0
