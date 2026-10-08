"""Implementations of the `dapier schedules` commands."""

from datetime import datetime
from urllib.parse import quote

from .. import api
from .shared import read_json_file

__all__ = ["schedules_delete", "schedules_list", "schedules_pause", "schedules_resume",
           "schedules_run", "schedules_save", "schedules_show", "schedules_upcoming"]

# The API's health states, as the CLI words them (the console uses the same).
HEALTH = {"ok": "on time", "waiting": "waiting", "attention": "NEEDS ATTENTION",
          "paused": "paused"}
OUTCOMES = {
    "ran": "ran",
    "no_listeners": "fired, no workflow listening",
    "failed": "failed",
    "retrying": "failed, retry queued",
    "paused": "paused",
    "resumed": "resumed",
}


def _when(value):
    """'Mon 09 Oct 09:00' (UTC) for an ISO time, or '' when absent."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return moment.strftime("%a %d %b %H:%M")


def schedules_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/schedule-triggers", debug=debug)
    items = data.get("schedules", [])
    if not items:
        print("No schedule triggers yet. Create one with `dapier schedules save`.")
    for item in items:
        health = item.get("health") or {}
        state = HEALTH.get(health.get("state")) or (
            "enabled" if item.get("enabled", True) else "disabled")
        nexts = item.get("next_runs") or []
        print(f"{item.get('schedule_id', ''):20} {item.get('expression', ''):28} "
              f"{state:15} {('next ' + _when(nexts[0])) if nexts else ''}")
        if item.get("summary"):
            print(f"{'':20} {item['summary']}")
        if health.get("state") == "attention" and health.get("reason"):
            print(f"{'':20} ! {health['reason']}")
    return 0


def schedules_show(api_url, name, debug=False):
    """One schedule: when it runs, what it reaches, and its recent fires."""
    data = api.call(api_url, "GET", "/api/agent/schedule-triggers", debug=debug)
    item = next((entry for entry in data.get("schedules", [])
                 if entry.get("schedule_id") == name), None)
    if not item:
        print(f"No schedule trigger named '{name}'.")
        return 1
    health = item.get("health") or {}
    print(f"{item['schedule_id']}  ({HEALTH.get(health.get('state'), health.get('state') or '')})")
    if item.get("description"):
        print(f"  {item['description']}")
    print(f"  When:      {item.get('summary') or ''}  [{item.get('expression')}]")
    if health.get("reason"):
        print(f"  Health:    {health['reason']}")
    workflows = item.get("workflows") or []
    if workflows:
        names = ", ".join(w["id"] + ("" if w.get("enabled", True) else " (off)") for w in workflows)
        print(f"  Runs:      {names}")
    else:
        print("  Runs:      no published workflow filters on this schedule")
    nexts = item.get("next_runs") or []
    approx = " (approximate)" if item.get("approximate") else ""
    print(f"  Next:      {', '.join(_when(n) for n in nexts) or '-'}{approx}")
    fires = item.get("fires") or []
    print("  Recent fires (UTC):")
    if not fires:
        print("    none recorded yet")
    for fire in fires:
        outcome = OUTCOMES.get(fire.get("outcome"), fire.get("outcome") or "")
        extra = ""
        if fire.get("workflows"):
            extra = " " + ", ".join(fire["workflows"])
        if fire.get("manual"):
            extra += "  (run now" + (f" by {fire['by']}" if fire.get("by") else "") + ")"
        elif fire.get("by"):
            extra += f"  by {fire['by']}"
        if fire.get("error"):
            extra += f"  - {fire['error']}"
        print(f"    {_when(fire.get('at')):18} {outcome}{extra}")
        for run in fire.get("runs") or []:
            print(f"      run {run['run_id']}")
    return 0


def schedules_upcoming(api_url, hours=24, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/schedule-triggers/upcoming?hours={int(hours)}",
                    debug=debug)
    upcoming = data.get("upcoming") or []
    frequent = data.get("frequent") or []
    span = f"{hours // 24} days" if hours % 24 == 0 and hours > 24 else f"{hours} hours"
    if not upcoming and not frequent:
        print(f"Nothing is scheduled to run in the next {span}.")
        return 0
    print(f"Next {span} (UTC):")
    day = None
    for entry in upcoming:
        when = _when(entry.get("at"))
        if when[:10] != day:
            day = when[:10]
            print(f"  {day}")
        reach = ", ".join(entry.get("workflows") or []) or "no workflow listening"
        approx = "~" if entry.get("approximate") else " "
        print(f"    {approx}{when[11:]}  {entry.get('schedule_id', ''):20} -> {reach}")
    if data.get("truncated"):
        print("  ... more fires in this window; narrow it with --hours.")
    for entry in frequent:
        reach = ", ".join(entry.get("workflows") or []) or "no workflow listening"
        print(f"  {entry.get('schedule_id', '')}: {entry.get('summary')} "
              f"({entry.get('count')} fires) -> {reach}")
    return 0


def schedules_run(api_url, name, debug=False):
    data = api.call(api_url, "POST", f"/api/agent/schedule-triggers/{quote(name)}/run",
                    {}, debug=debug)
    workflows = data.get("workflows") or []
    print(f"Queued a run of schedule '{data.get('schedule_id', name)}' "
          f"(event {data.get('event_id')}).")
    if workflows:
        for run in data.get("runs") or []:
            print(f"  {run['workflow_id']}: dapier runs show {run['run_id']}")
    else:
        print("  No published workflow listens to it, so nothing will run.")
    return 0


def _set_enabled(api_url, name, action, debug):
    data = api.call(api_url, "POST", f"/api/agent/schedule-triggers/{quote(name)}/{action}",
                    {}, debug=debug)
    state = "paused" if action == "pause" else "resumed"
    if not data.get("changed", True):
        print(f"Schedule '{data.get('schedule_id', name)}' was already {state}.")
    else:
        print(f"Schedule '{data.get('schedule_id', name)}' {state}; "
              f"its EventBridge rule is {'DISABLED' if action == 'pause' else 'ENABLED'}.")
    return 0


def schedules_pause(api_url, name, debug=False):
    return _set_enabled(api_url, name, "pause", debug)


def schedules_resume(api_url, name, debug=False):
    return _set_enabled(api_url, name, "resume", debug)


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
