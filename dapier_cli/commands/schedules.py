"""Implementations of the `dapier schedules` commands."""

from .. import api
from .shared import read_json_file

__all__ = ["schedules_delete", "schedules_list", "schedules_save"]


def schedules_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/schedule-triggers", debug=debug)
    items = data.get("schedules", [])
    if not items:
        print("No schedule triggers yet. Create one with `dapier schedules save`.")
    for item in items:
        state = "enabled" if item.get("enabled", True) else "disabled"
        print(f"{item.get('schedule_id', ''):20} {item.get('expression', ''):40} "
              f"{state:9}")
    return 0


def schedules_save(api_url, path, debug=False):
    body, error = read_json_file(path)
    if error:
        print(error)
        return 2
    data = api.call(api_url, "PUT", "/api/agent/schedule-triggers", body, debug=debug)
    verb = "Created" if data.get("created") else "Updated"
    state = "enabled" if data.get("enabled", True) else "disabled"
    print(f"{verb} schedule '{data.get('schedule_id')}' ({data.get('expression')}, {state}).")
    print(f"  EventBridge rule: {data.get('rule')}")
    print("  It fires on the worker immediately per the schedule; no deploy needed.")
    return 0


def schedules_delete(api_url, name, debug=False):
    api.call(api_url, "DELETE", f"/api/agent/schedule-triggers?name={name}", debug=debug)
    print(f"Deleted schedule trigger '{name}' and its EventBridge rule.")
    return 0


