"""Presence registry for host workers (`dapier worker` processes).

A worker checks in on every claim poll and task heartbeat, so the console's
Workers tab and `dapier workers list` can show which machines are picking up
agent tasks — and whether any is running at all (idle tasks stay queued
until one is). Rows live beside the agent tasks they run, in the same table
with a ``worker:`` id prefix and ``kind: worker``; a worker that stops
checking in drops out through the table TTL instead of lingering forever.
"""

import time

from .worker_capabilities import capabilities

from .host_tasks import tasks_table

# Claim long-polls every ~10s and heartbeats every 40s while a task runs,
# so a two-minute silence means the process is gone.
ACTIVE_WINDOW_SECONDS = 120
TTL_SECONDS = 14 * 86400
PUBLIC_FIELDS = (
    "worker_id", "hostname", "pid", "workspace_root", "owner",
    "started_at", "last_seen", "current_task_id", "last_task_id", "last_status",
)


def meta_of(body):
    """The ``worker`` identity block a `dapier worker` sends with each call.

    None when the caller is an older worker or not a worker at all —
    presence is strictly opt-in so the lease protocol stays backward
    compatible.
    """
    meta = body.get("worker") if isinstance(body, dict) else None
    if not isinstance(meta, dict):
        return None
    worker_id = str((meta or {}).get("worker_id") or "").strip()
    if not worker_id:
        return None
    pid = meta.get("pid")
    return {
        "worker_id": worker_id[:200],
        "hostname": str(meta.get("hostname") or "")[:255],
        "pid": pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
        "workspace_root": str(meta.get("workspace_root") or "")[:400],
        **({"capabilities": capabilities(meta["capabilities"])} if "capabilities" in meta else {}),
        **({"engine": meta["engine"]} if meta.get("engine") in ("claude", "codex") else {}),
    }


def checkin(owner, meta, *, task_id=None, finished=None, table_ref=None, now=None):
    """Upsert one worker's presence row.

    ``task_id`` marks the task a worker just leased or is still running;
    ``finished`` is a ``(task_id, status)`` pair that clears the current
    task and records the outcome.
    """
    table = tasks_table(table_ref)
    current = int(now or time.time())
    names, values, assignments = {}, {}, [
        "#kind = :kind", "#seen = :seen", "expires_at = :expires",
        "#owner = :owner", "#worker = :wid",
        "started_at = if_not_exists(started_at, :now)",
    ]
    values.update({":kind": "worker", ":seen": current,
                   ":expires": current + TTL_SECONDS,
                   ":owner": str(owner or ""), ":now": current,
                   ":wid": meta["worker_id"]})
    # owner (and, defensively, worker_id) are DynamoDB reserved words; kind
    # and last_seen ride names for symmetry with the mapped keys below.
    names.update({"#kind": "kind", "#seen": "last_seen", "#owner": "owner",
                  "#worker": "worker_id"})
    for key in ("hostname", "pid", "workspace_root", "capabilities", "engine"):
        if meta.get(key) is not None:
            names[f"#{key}"] = key
            values[f":{key}"] = meta[key]
            assignments.append(f"#{key} = :{key}")
    if task_id:
        assignments.append("current_task_id = :task")
        values[":task"] = task_id
    if finished:
        assignments.extend(["current_task_id = :idle", "last_task_id = :done",
                            "last_status = :state"])
        values.update({":idle": "", ":done": finished[0], ":state": finished[1]})
    table.update_item(
        Key={"task_id": f"worker:{meta['worker_id']}"},
        UpdateExpression="SET " + ", ".join(assignments),
        ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def api_list(table_ref=None, now=None):
    """Every known worker, most recently seen first: ``(status, payload)``.

    Each row carries ``active`` — seen inside the activity window — so the
    console and CLI render the same judgment without their own clocks.
    """
    current = int(now or time.time())
    table = tasks_table(table_ref)
    items, start = [], None
    while True:
        page = table.scan(**({"ExclusiveStartKey": start} if start else {}))
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            break
    workers = []
    for item in items:
        if item.get("kind") != "worker":
            continue
        worker = {key: item.get(key) for key in PUBLIC_FIELDS}
        # Rows written before the id was stored as an attribute still carry it
        # in the row key; fall back so they don't render a blank worker.
        if not worker["worker_id"]:
            worker["worker_id"] = str(item.get("task_id") or "").removeprefix("worker:")
        worker["capabilities"] = item.get("capabilities") or []
        worker["engine"] = item.get("engine") or "claude"
        worker["active"] = int(item.get("last_seen") or 0) > current - ACTIVE_WINDOW_SECONDS
        workers.append(worker)
    workers.sort(key=lambda worker: (int(worker.get("last_seen") or 0),
                                     str(worker.get("worker_id") or "")), reverse=True)
    return 200, {"workers": workers, "active_window": ACTIVE_WINDOW_SECONDS}
