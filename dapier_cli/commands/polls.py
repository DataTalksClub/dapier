"""Implementations of the `dapier polls` commands."""


from urllib.parse import quote, urlencode

from .. import api
from .shared import read_json_file

__all__ = ["polls_activity", "polls_check", "polls_delete", "polls_list", "polls_reset",
           "polls_save", "polls_set_enabled", "polls_show"]

# The health words the API returns, in the console's plain language.
HEALTH_TEXT = {
    "ok": "ok",
    "failing": "failing",
    "paused": "paused",
    "waiting": "not checked yet",
    "late": "late (missed checks)",
}


def _poll_target(item):
    """What a poll watches: the URL for http, the provider target otherwise."""
    if item.get("source") and item.get("source") != "http":
        target = (item.get("bucket") or item.get("spreadsheet_id")
                  or item.get("folder_id") or item.get("for_email") or "")
        return f"{item['source']} {target}".strip()
    return f"{item.get('method', 'GET')} {item.get('url', '')}".strip()


def _when(value):
    return (str(value or "")[:16].replace("T", " ")) or "never"


def _workflows_text(item):
    flows = item.get("workflows") or []
    if not flows:
        return "no workflow"
    return ", ".join(flow.get("id", "") + ("" if flow.get("enabled", True) else " (off)")
                     for flow in flows)


def polls_list(api_url, debug=False):
    data = api.call(api_url, "GET", "/api/agent/poll-triggers", debug=debug)
    items = data.get("polls", [])
    if not items:
        print("No poll triggers yet. Create one with `dapier polls save`.")
        return 0
    print(f"{'POLL':24} {'HEALTH':20} {'EVERY':20} {'LAST CHECK':17} {'LAST NEW':17} WATCHES")
    for item in items:
        status = item.get("status") or {}
        health = item.get("health") or ("enabled" if item.get("enabled", True) else "paused")
        health = HEALTH_TEXT.get(health, health)
        print(f"{item.get('poll_id', ''):24} {health:20} {item.get('expression', ''):20} "
              f"{_when(status.get('last_checked_at')):17} {_when(status.get('last_new_at')):17} "
              f"{_poll_target(item)}")
        if item.get("health") == "failing" and status.get("last_error"):
            print(f"    last error: {status['last_error']}")
        print(f"    starts: {_workflows_text(item)}")
    return 0


def polls_show(api_url, name, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/poll-triggers/{quote(name, safe='')}", debug=debug)
    item = data.get("poll") or {}
    status = item.get("status") or {}
    print(f"poll: {item.get('poll_id', name)}")
    print(f"  health: {HEALTH_TEXT.get(item.get('health'), item.get('health'))}")
    print(f"  watches: {_poll_target(item)}")
    print(f"  schedule: {item.get('expression', '')}")
    print(f"  starts: {_workflows_text(item)}")
    print(f"  last check: {_when(status.get('last_checked_at'))}"
          + (f" ({status['last_reason']})" if status.get("last_reason") else "")
          + (f", {status.get('last_found', 0)} new" if status.get("last_checked_at") else ""))
    print(f"  last new item: {_when(status.get('last_new_at'))}"
          + (f" ({status['last_new_count']} items)" if status.get("last_new_count") else ""))
    if status.get("failures"):
        print(f"  failing: {status['failures']} check(s) in a row")
    if status.get("last_error"):
        print(f"  last error: {_when(status.get('last_error_at'))} — {status['last_error']}")
    print(f"  position: {item.get('cursor') if item.get('cursor') is not None else '(not started)'}")
    print(f"  resets: {', '.join(item.get('reset_modes') or ['now'])}")
    if item.get("description"):
        print(f"  description: {item['description']}")
    return 0


# The plain-words outcomes `dapier inbox list` and the console use.
OUTCOME_TEXT = {
    "matched": "handled",
    "unmatched": "unmatched",
    "failed": "failed",
    "received": "pending",
    "ignored": "ignored",
}


def polls_activity(api_url, name=None, limit=25, debug=False):
    params = {"limit": int(limit)}
    if name:
        params["name"] = name
    data = api.call(api_url, "GET", f"/api/agent/poll-triggers/activity?{urlencode(params)}",
                    debug=debug)
    items = data.get("items", [])
    if not items:
        print("No items picked up yet." if not name else f"Poll '{name}' has not picked up any items yet.")
        return 0
    print(f"{'PICKED UP':17} {'POLL':24} {'OUTCOME':12} ITEM")
    for item in items:
        outcome = OUTCOME_TEXT.get(item.get("status"), item.get("status") or "-")
        print(f"{_when(item.get('received_at')):17} {item.get('poll') or '-':24} "
              f"{outcome:12} {item.get('title') or item.get('item_id') or '-'}")
        for run in item.get("runs") or []:
            print(f"    run: {run}")
        if item.get("error"):
            print(f"    error: {item['error']}")
        print(f"    inbox: {item.get('inbox_id')}  (`dapier inbox show|replay <id>`)")
    return 0


def polls_check(api_url, name, debug=False):
    api.call(api_url, "POST", f"/api/agent/poll-triggers/{quote(name, safe='')}/check",
             body={}, debug=debug)
    print(f"Queued a check of poll '{name}'. New items start their workflows; "
          f"see `dapier polls show {name}` in a few seconds.")
    return 0


def polls_set_enabled(api_url, name, enabled, debug=False):
    action = "resume" if enabled else "pause"
    api.call(api_url, "POST", f"/api/agent/poll-triggers/{quote(name, safe='')}/{action}",
             body={}, debug=debug)
    print(f"Poll '{name}' {'resumed — it checks on its schedule again' if enabled else 'paused — no checks until you resume it'}.")
    return 0


def polls_reset(api_url, name, date=None, debug=False):
    body = {"to": "date", "date": date} if date else {"to": "now"}
    data = api.call(api_url, "POST", f"/api/agent/poll-triggers/{quote(name, safe='')}/reset",
                    body=body, debug=debug)
    if date:
        print(f"Poll '{name}' will pick up items newer than {data.get('cursor')} on its next check.")
    else:
        skipped = data.get("skipped", 0)
        print(f"Poll '{name}' now starts from now: {skipped} waiting item(s) skipped.")
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


