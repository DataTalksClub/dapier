"""Implementations of the `dapier runs` commands."""

import json, os
from urllib.parse import quote
from urllib.parse import urlencode

from .. import api

__all__ = ["print_runs", "runs_cancel", "runs_export", "runs_list", "runs_replay",
           "runs_replay_failed", "runs_resolve", "runs_show"]


def print_runs(items):
    print(f"{'RUN':42} {'STATUS':12} {'STEPS':5} STARTED")
    for item in items:
        started = (item.get("started_at") or "-")[:19]
        # A resolved failure keeps its failed status; "fixed" says it no
        # longer needs action, and why — the operator marked it, or a later
        # run of the workflow completed.
        status = item.get("status", "")
        if item.get("resolved"):
            status = f"{status} (fixed: {item.get('resolved_reason') or 'resolved'})"
        print(f"{item.get('run_id', ''):42} {status:12} "
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
              next_token=None, query=None, resolved=None, debug=False):
    params = {"limit": int(limit)}
    if workflow:
        params["workflow_id"] = workflow
    # --resolved / --unresolved are the two halves of the failure set, which
    # the API spells `problems` and `resolved`.
    if resolved is True:
        status = "resolved"
    elif resolved is False:
        status = "problems"
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
    paging = data.get("paging") or {}
    # The server flags a window its scan budget clipped. An empty list under
    # a clipped window means "none where we looked", which is the one case
    # that must never read as a clean bill of health - the failures query is
    # exactly the one an operator trusts when it comes back empty.
    clipped = bool(paging.get("bounded"))
    if not items:
        filtered = (workflow or status or since or before or next_token or query
                    or resolved is not None)
        print("No runs match these filters." if filtered else
              "No runs recorded yet. Runs appear once a workflow handles a trigger event.")
        if clipped:
            print("The search window was clipped; older rows may not have been reached.")
        return 0
    print_runs(items)
    if paging.get("next"):
        print(f"\nnext page: {paging['next']}  (pass it to --next)")
    if clipped:
        # The server's scan budget clipped the window, so with filters on,
        # "no rows" means "none in the part searched" - say so rather than
        # letting an empty list read as a clean bill of health.
        print("note: the search window was clipped; older rows may not have been reached.")
    return 0


def runs_export(api_url, out=None, max_rows=None, workflow=None, status=None,
                since=None, before=None, query=None, resolved=None, debug=False):
    """Run history as CSV (thin client over the agent export route): the
    list's filters, one bounded export, written to --out or the server's
    suggested filename."""
    params = {}
    if workflow:
        params["workflow_id"] = workflow
    if resolved is True:
        status = "resolved"
    elif resolved is False:
        status = "problems"
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
    if run.get("resolved"):
        print(f"resolved: {run.get('resolved_reason') or 'resolved'}"
              + (f" at {run['resolved_at']}" if run.get("resolved_at") else "")
              + (f" by {run['resolved_by']}" if run.get("resolved_by") else ""))
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


def runs_resolve(api_url, run_id, note=None, debug=False):
    """Mark a failed run fixed (thin client over the agent resolve route).

    The failure keeps its status and error in history but stops counting as
    a problem. Use it for a failure nothing will re-derive away: a dead
    workflow's last run, a negative test meant to fail. A failure a rerun
    already fixed needs nothing here — it resolves itself.
    """
    data = api.call(api_url, "POST", f"/api/agent/runs/{quote(run_id, safe='')}/resolve",
                    body={"note": note} if note else {}, debug=debug)
    if data.get("already_resolved"):
        print(f"{run_id} was already fixed ({data.get('resolved_reason') or 'resolved'}"
              + (f" at {data['resolved_at']}" if data.get("resolved_at") else "") + ").")
        return 0
    print(f"Marked {run_id} fixed: {data.get('steps', 0)} step record(s) stamped.")
    print("The run keeps its failed status and error in history, but it no longer "
          "counts as a problem.")
    print("A new failure of the same workflow is a new run and starts unresolved again.")
    return 0


def runs_replay_failed(api_url, workflow_id, debug=False):
    data = api.call(api_url, "POST", "/api/agent/runs/replay-failed",
                    {"workflow_id": workflow_id}, debug=debug)
    print(f"Replay accepted for {data.get('replayed', 0)} unresolved failed run(s) of "
          f"{workflow_id}"
          + (f"; {data.get('skipped', 0)} skipped." if data.get("skipped") else "."))
    for item in data.get("runs") or []:
        if item.get("replayed"):
            print(f"  {item.get('run_id')}: replayed as {item.get('new_run_id')}")
        else:
            print(f"  {item.get('run_id')}: skipped ({item.get('reason')})")
    print("The re-injected runs appear in `dapier runs list` once the worker picks them up.")
    return 0


