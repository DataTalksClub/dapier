"""Implementations of the `dapier polls` commands."""


from .. import api
from .shared import read_json_file

__all__ = ["polls_delete", "polls_list", "polls_save"]


def _poll_target(item):
    """What a poll watches: the URL for http, the provider target otherwise."""
    if item.get("source") and item.get("source") != "http":
        target = (item.get("bucket") or item.get("spreadsheet_id")
                  or item.get("folder_id") or item.get("for_email") or "")
        return f"{item['source']} {target}".strip()
    return f"{item.get('method', 'GET')} {item.get('url', '')}".strip()


def polls_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/poll-triggers", debug=debug)
    items = data.get("polls", [])
    if not items:
        print("No poll triggers yet. Create one with `dapier polls save`.")
    for item in items:
        state = "enabled" if item.get("enabled", True) else "disabled"
        print(f"{item.get('poll_id', ''):20} {item.get('expression', ''):40} "
              f"{state:9} {_poll_target(item)}")
    return 0


def polls_save(api_url, path, debug=False):
    body, error = read_json_file(path)
    if error:
        print(error)
        return 2
    data = api.call(api_url, "PUT", "/api/agent/poll-triggers", body, debug=debug)
    verb = "Created" if data.get("created") else "Updated"
    state = "enabled" if data.get("enabled", True) else "disabled"
    print(f"{verb} poll trigger '{data.get('poll_id')}' ({data.get('expression')}, {state}).")
    print(f"  EventBridge rule: {data.get('rule')}")
    print(f"  Watches {_poll_target(data)} — one event per new item, no deploy needed.")
    return 0


def polls_delete(api_url, name, debug=False):
    api.call(api_url, "DELETE", f"/api/agent/poll-triggers?name={name}", debug=debug)
    print(f"Deleted poll trigger '{name}' and its EventBridge rule.")
    return 0


