"""Implementations of the `dapier grants` commands."""

from urllib.parse import urlencode

from .. import api
from .shared import read_json_file

__all__ = ["grants_delete", "grants_list", "grants_save", "print_grants"]


def print_grants(items):
    print(f"{'CONNECTION':24} {'SUBJECT':34} {'AGENT':24} {'OPERATIONS':18} EXPIRES")
    for item in items:
        operations = ",".join(item.get("operations") or [])
        expires = item.get("expires_at") or "-"
        print(f"{item.get('connection_id', ''):24} {item.get('subject', ''):34} "
              f"{item.get('agent', ''):24} {operations:18} {expires}")


def grants_list(api_url, connection_id=None, debug=False, limit=None, next_token=None):
    params = {}
    if connection_id:
        params["connection_id"] = connection_id
    if limit:
        params["limit"] = int(limit)
    if next_token:
        params["next"] = next_token
    query = f"?{urlencode(params)}" if params else ""
    data = api.call(api_url, "GET", f"/api/agent/grants{query}", debug=debug)
    items = data.get("grants", [])
    if not items:
        print("No grants. Create one with `dapier grants save`.")
        return 0
    print_grants(items)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
    return 0


def grants_save(api_url, path, debug=False):
    body, error = read_json_file(path)
    if error:
        print(error)
        return 2
    data = api.call(api_url, "PUT", "/api/agent/grants", body, debug=debug)
    print(f"Granted {data.get('subject')}#{data.get('agent')} on {data.get('connection_id')} "
          f"({', '.join(data.get('operations') or [])}).")
    return 0


def grants_delete(api_url, connection_id, grantee, debug=False):
    query = urlencode({"connection_id": connection_id, "grantee": grantee})
    api.call(api_url, "DELETE", f"/api/agent/grants?{query}", debug=debug)
    print(f"Revoked {grantee} on {connection_id}.")
    return 0


