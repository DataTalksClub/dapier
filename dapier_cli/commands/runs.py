"""Implementations of the `dapier runs` commands."""

import json, os
from urllib.parse import quote
from urllib.parse import urlencode

from .. import api

__all__ = ["print_runs", "runs_cancel", "runs_export", "runs_list", "runs_replay", "runs_replay_failed", "runs_show"]


def print_runs(items):
    print(f"{'RUN':42} {'STATUS':12} {'STEPS':5} STARTED")
    for item in items:
        started = (item.get("started_at") or "-")[:19]
        print(f"{item.get('run_id', ''):42} {item.get('status', ''):12} "
              f"{item.get('steps', 0):<5} {started}")
        # The same what-actually-happened line the console row carries:
        # trigger type plus the API's event summary, indented under the run.
        trigger = " · ".join(str(part) for part in
                             (item.get("connector"), item.get("event_type")) if part)
        summary = str(item.get("event_summary") or "")
        detail = " — ".join(part for part in (trigger, summary) if part)
        if detail:
            print(f"    {detail}")


def runs_list(api_url, limit=25, workflow=None, status=None, since=None, before=None,
              next_token=None, query=None, debug=False):
    params = {"limit": int(limit)}
    if workflow:
        params["workflow_id"] = workflow
    if status:
        params["status"] = status
    if since:
        params["since"] = since
    if before:
        params["before"] = before
    if query:
        params["q"] = query
    if next_token:
        params["next"] = next_token
    data = api.call(api_url, "GET", f"/api/agent/runs?{urlencode(params)}", debug=debug)
    items = data.get("runs", [])
    if not items:
        filtered = workflow or status or since or before or next_token or query
        print("No runs match these filters." if filtered else
              "No runs recorded yet. Runs appear once a workflow handles a trigger event.")
        return 0
    print_runs(items)
    next_page = (data.get("paging") or {}).get("next")
    if next_page:
        print(f"\nnext page: {next_page}  (pass it to --next)")
    return 0


def runs_export(api_url, out=None, max_rows=None, workflow=None, status=None,
                since=None, before=None, query=None, debug=False):
    """Run history as CSV (thin client over the agent export route): the
    list's filters, one bounded export, written to --out or the server's
    suggested filename."""
    params = {}
    if workflow:
        params["workflow_id"] = workflow
    for key, value in (("status", status), ("since", since),
                       ("before", before), ("q", query)):
        if value:
            params[key] = value
    if max_rows:
        params["max_rows"] = int(max_rows)
    data = api.call(api_url, "GET", f"/api/agent/runs/export?{urlencode(params)}",
                    debug=debug)
    # The server suggests the filename; basename keeps a hostile suggestion
    # from writing outside the caller's directory.
    path = out or os.path.basename(data.get("filename") or "") or "dapier-runs.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(data.get("csv") or "")
    note = " (capped; narrow the filters for the rest)" if data.get("truncated") else ""
    print(f"Wrote {data.get('count', 0)} runs to {path}{note}")
    return 0


def runs_show(api_url, run_id, debug=False):
    data = api.call(api_url, "GET", f"/api/agent/runs/{quote(run_id, safe='')}", debug=debug)
    run = data.get("run") or {}
    print(f"run: {run.get('run_id') or run_id}")
    for key in ("workflow_id", "status", "connector", "event_type", "steps",
                "started_at", "finished_at", "duration_ms", "failed_step", "error"):
        if run.get(key) not in (None, ""):
            print(f"{key}: {run[key]}")
    for index, step in enumerate(data.get("steps") or [], 1):
        label = step.get("action_id") or "?"
        if step.get("action_type"):
            label = f"{label} ({step['action_type']})"
        print(f"\nstep[{index}]: {label}  {step.get('status', '?')}"
              + (f"  {step['duration_ms']}ms" if step.get("duration_ms") is not None else ""))
        if step.get("error"):
            print(f"  error: {step['error']}")
        for field in ("input", "output"):
            value = step.get(field)
            if value not in (None, "", [], {}):
                print(f"  {field}: {json.dumps(value, sort_keys=True, default=str)}")
    return 0


def runs_replay(api_url, run_id, from_step=None, debug=False):
    body = {"from_step": from_step} if from_step else {}
    data = api.call(api_url, "POST", f"/api/agent/runs/{quote(run_id, safe='')}/replay",
                    body=body, debug=debug)
    if from_step:
        print(f"Replay from step '{from_step}' accepted for {data.get('replayed_from') or run_id}.")
        print("Earlier steps do not run again; their recorded outputs seed the rerun.")
    else:
        print(f"Replay accepted for {data.get('replayed_from') or run_id}.")
    print(f"The re-injected run ({data.get('run_id') or 'pending'}) appears in `dapier runs list` "
          "once the worker picks it up.")
    return 0


def runs_cancel(api_url, run_id, debug=False):
    data = api.call(api_url, "POST", f"/api/agent/runs/{quote(run_id, safe='')}/cancel", body={}, debug=debug)
    run = data.get("run") or {}
    print(f"Cancelled {data.get('cancelled', 0)} parked step(s) of "
          f"{data.get('run_id') or run_id}; the run reads `{run.get('status', 'cancelled')}`.")
    print("The parked continuation is dropped: the run's remaining actions will not fire.")
    return 0


def runs_replay_failed(api_url, workflow_id, debug=False):
    data = api.call(api_url, "POST", "/api/agent/runs/replay-failed",
                    {"workflow_id": workflow_id}, debug=debug)
    print(f"Replay accepted for {data.get('replayed', 0)} failed run(s) of {workflow_id}"
          + (f"; {data.get('skipped', 0)} skipped." if data.get("skipped") else "."))
    for item in data.get("runs") or []:
        if item.get("replayed"):
            print(f"  {item.get('run_id')}: replayed as {item.get('new_run_id')}")
        else:
            print(f"  {item.get('run_id')}: skipped ({item.get('reason')})")
    print("The re-injected runs appear in `dapier runs list` once the worker picks them up.")
    return 0


