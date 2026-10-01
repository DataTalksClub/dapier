"""Implementations of the `dapier usage`, `dapier quota`, and `dapier errors` commands."""


from .. import api

__all__ = ["errors_send_digest", "errors_summary", "quota", "usage"]


def _print_quota(quota):
    """One line for the quota block the usage and quota payloads carry."""
    if not quota or not quota.get("enabled"):
        used = (quota or {}).get("used", 0)
        print(f"Task quota: none set ({used} tasks this month — uncapped)")
        return
    used, limit = quota.get("used", 0), quota.get("limit")
    remaining = quota.get("remaining")
    tail = f", {remaining} left" if remaining is not None else ""
    print(f"Task quota: {used}/{limit} tasks this month ({quota.get('month', '')}{tail})")


def usage(api_url, debug=False, months=12):
    data = api.call(api_url, "GET", f"/api/agent/usage?months={int(months)}", debug=debug)
    items = data.get("usage", [])
    if not items:
        print("No task usage recorded yet. Usage appears once workflow actions complete.")
    else:
        print(f"{'MONTH':8} {'WORKFLOW':40} TASKS")
        for item in items:
            print(f"{item.get('month', ''):8} {item.get('workflow_id', ''):40} {item.get('tasks', 0)}")
    _print_quota(data.get("quota"))
    return 0


def quota(api_url, debug=False, command="show", limit=None):
    """Show the monthly task quota, or set it: `dapier quota set 1000` /
    `dapier quota set off` (the worker fails action steps once the month's
    budget is spent)."""
    if command == "set":
        data = api.call(api_url, "PUT", "/api/agent/quota",
                        body={"limit": limit}, debug=debug)
        _print_quota((data or {}).get("quota"))
        return 0
    data = api.call(api_url, "GET", "/api/agent/quota", debug=debug)
    _print_quota((data or {}).get("quota"))
    return 0


def errors_summary(api_url, debug=False, days=7):
    data = api.call(api_url, "GET", f"/api/agent/errors/summary?days={int(days)}", debug=debug)
    workflows = data.get("workflows") or []
    if not workflows:
        print(f"No failed runs in the last {data.get('window_days', int(days))} days.")
        return 0
    print(f"Failed runs by workflow (last {data.get('window_days', int(days))} days, "
          f"{data.get('total_failed_runs', 0)} total)")
    print(f"{'WORKFLOW':40} {'FAILED RUNS':11} LAST FAILURE")
    for item in workflows:
        last = (item.get('last_failed_at') or '-')[:19]
        print(f"{item.get('workflow_id', ''):40} {item.get('failed_runs', 0):<11} {last}")
        if item.get("last_error"):
            print(f"{'':40} {'':11} {item['last_error']}")
    return 0


def errors_send_digest(api_url, debug=False):
    """Render and email the operator error digest now (thin client over the
    agent route — the same function the daily schedule runs)."""
    data = api.call(api_url, "POST", "/api/agent/errors/digest", body={}, debug=debug)
    if data.get("skipped"):
        print(f"Nothing failed in the last {data.get('window_days', 1)} day(s); "
              "digest skipped (no email sent).")
        return 0
    print(f"Digest sent to {data.get('to', '')} "
          f"({data.get('total_failed_runs', 0)} failed runs in the last "
          f"{data.get('window_days', 1)} day(s)).")
    if data.get("subject"):
        print(data["subject"])
    return 0


