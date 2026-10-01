"""Implementations of the `dapier storage` commands."""

from urllib.parse import quote

from .. import api

__all__ = ["storage_delete", "storage_find", "storage_get", "storage_set"]


def storage_get(api_url, workflow, key, debug=False):
    data = api.call(api_url, "GET",
                    f"/api/agent/storage/{quote(workflow, safe='')}?key={quote(key, safe='')}",
                    debug=debug)
    print(f"{data.get('key') or key} = {data.get('value', '')}")
    for stamp in ("updated_at", "expires"):
        if data.get(stamp):
            print(f"  {stamp}: {data[stamp]}")
    return 0


def storage_set(api_url, workflow, key, value, ttl_seconds=None, debug=False):
    body = {"key": key, "value": value}
    if ttl_seconds is not None:
        body["ttl_seconds"] = int(ttl_seconds)
    data = api.call(api_url, "POST", f"/api/agent/storage/{quote(workflow, safe='')}",
                    body=body, debug=debug)
    suffix = f" (expires {data['expires']})" if data.get("expires") else ""
    print(f"Stored {data.get('key') or key} for {workflow}{suffix}.")
    print("Workflow runs read it back with the storage_get action (`{steps.<id>.output.value}`).")
    return 0


def storage_find(api_url, workflow, prefix="", limit=None, debug=False):
    query = f"prefix={quote(prefix, safe='')}"
    if limit:
        query += f"&limit={int(limit)}"
    data = api.call(api_url, "GET",
                    f"/api/agent/storage/{quote(workflow, safe='')}?{query}", debug=debug)
    items = data.get("items", [])
    if not items:
        print(f"No stored keys under '{prefix}' for {workflow}.")
        return 0
    print(f"{'KEY':40} VALUE")
    for item in items:
        print(f"{item.get('key', ''):40} {item.get('value', '')}")
    return 0


def storage_delete(api_url, workflow, key, debug=False):
    data = api.call(api_url, "DELETE",
                    f"/api/agent/storage/{quote(workflow, safe='')}?key={quote(key, safe='')}",
                    debug=debug)
    if data.get("deleted"):
        print(f"Deleted {key} from {workflow}'s storage.")
    else:
        print(f"{key} was not stored for {workflow} (nothing to delete).")
    return 0


