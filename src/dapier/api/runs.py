"""Run history: one run per workflow handling of a trigger event, with the
per-step flow (status, input, output, duration, error) between elements.

Replay re-injects a past run's original trigger event onto the event queue —
the same dispatch path every hook and custom event takes — so the rerun
lands in run history exactly like a normal run, for failed runs (retry) and
successful ones (replay) alike.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key

GSI_NAME = "runs-by-run-id"

STEP_FIELDS = (
    "execution_id", "run_id", "workflow_id", "action_id", "action_type",
    "connector", "event_type", "correlation_id", "status", "started_at",
    "finished_at", "occurred_at", "duration_ms", "error", "input", "output",
    "expires_at",
)


def _table():
    return boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"])


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def run_id_of(item):
    """The run an execution belongs to.

    Records written before run grouping lack run_id; execution_id is
    ``workflow:action:event`` so the run id falls out by dropping the middle.
    """
    if item.get("run_id"):
        return item["run_id"]
    parts = str(item.get("execution_id", "")).split(":")
    if len(parts) >= 3:
        return f"{parts[0]}:{parts[-1]}"
    return item.get("execution_id") or ""


def run_summary(run_id, items):
    """Roll a run's steps up to one list row: worst status wins."""
    statuses = [item.get("status", "") for item in items]
    if any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status != "completed" for status in statuses):
        status = "processing"
    else:
        status = "completed"
    started = min(str(item.get("started_at") or "") for item in items)
    finished = max(str(item.get("finished_at") or "") for item in items)
    first = min(items, key=lambda item: str(item.get("started_at") or ""))
    failed = next((item for item in items if item.get("status") == "failed"), None)
    errors = [item.get("error") for item in items if item.get("error")]
    return {
        "run_id": run_id,
        "workflow_id": first.get("workflow_id") or run_id.split(":")[0],
        "connector": first.get("connector"),
        "event_type": first.get("event_type"),
        "status": status,
        "steps": len(items),
        "failed_step": failed.get("action_id") if failed else None,
        "started_at": started or None,
        "finished_at": finished or None,
        "duration_ms": _sum_duration(items),
        "error": errors[0] if errors else None,
    }


def _sum_duration(items):
    total = sum(_int(item.get("duration_ms")) or 0 for item in items)
    return total or None


def _step_view(item):
    view = {field: item.get(field) for field in STEP_FIELDS}
    view["duration_ms"] = _int(item.get("duration_ms"))
    view["expires_at"] = _int(item.get("expires_at"))
    return view


def recent(limit=25):
    """The most recent runs, newest first.

    A single bounded scan groups into runs; runs older than the scan window
    age out of the list but stay reachable through api_get. Failure-notice
    bookkeeping items (written by the worker's notification path) never
    group into the list.
    """
    items = _table().scan(Limit=max(limit * 6, 150)).get("Items", [])
    grouped = {}
    for item in items:
        if item.get("kind") == "failure-notice":
            continue
        grouped.setdefault(run_id_of(item), []).append(item)
    runs = [run_summary(run_id, group) for run_id, group in grouped.items()]
    runs.sort(key=lambda run: run.get("started_at") or "", reverse=True)
    return runs[:limit]


def api_list(limit=25):
    limit = max(1, min(_int(limit) or 25, 100))
    return 200, {"runs": recent(limit)}


def api_get(run_id):
    run_id = str(run_id or "").strip()
    if not run_id:
        return 400, {"error": "run_id is required"}
    items = _table().query(
        IndexName=GSI_NAME,
        KeyConditionExpression=Key("run_id").eq(run_id),
    ).get("Items", [])
    if not items:
        # Rows written before run grouping carry no run_id for the GSI to
        # index; the fallback scan sees only those (they age out with the
        # 90-day TTL once run_id-written rows dominate the table).
        items = [
            item for item in _table().scan(
                FilterExpression="attribute_not_exists(run_id)", Limit=500,
            ).get("Items", [])
            if run_id_of(item) == run_id
        ]
    if not items:
        return 404, {"error": "Run not found"}
    items.sort(key=lambda item: (str(item.get("started_at") or ""), str(item.get("execution_id") or "")))
    return 200, {
        "run": run_summary(run_id, items),
        "steps": [_step_view(item) for item in items],
    }


def replay_event(run_id, steps):
    """Rebuild the original trigger envelope from a run's stored steps.

    Every step of a run records the same event data as its input, so the
    first step with data carries the trigger. The replay gets a fresh event
    id (a fresh run in history) while ``correlation_id`` keeps the original
    event id, tying the rerun to the run it came from.

    Returns ``(event, None)``, or ``(None, error)`` when the stored steps
    cannot be replayed.
    """
    original_event_id = run_id.split(":", 1)[1] if ":" in run_id else run_id
    first = next(
        (step for step in steps if step.get("input") not in (None, "", {}, [])),
        None,
    )
    if first is None:
        return None, "This run's event data was not recorded; it cannot be replayed"
    data = first.get("input")
    if isinstance(data, dict) and data.get("truncated"):
        return None, "The original event data was too large to store; it cannot be replayed"
    event_id = f"replay-{uuid.uuid4()}"
    return {
        "schema_version": "1.0",
        "id": event_id,
        "correlation_id": original_event_id,
        "connector": first.get("connector"),
        "event": first.get("event_type"),
        "source": first.get("connector"),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": data if isinstance(data, dict) else {},
    }, None


def _queue():
    import boto3

    return boto3.client("sqs")


def api_replay(run_id, *, queue=None):
    """Re-execute a past run by re-injecting its original trigger event.

    The rebuilt envelope is published to the event queue, so the worker
    picks it up through the normal path: workflows are matched afresh and
    the rerun is recorded in run history like any other run. Asynchronous,
    hence 202; the response projects the replayed run's id from the
    original run's workflow.
    """
    run_id = str(run_id or "").strip()
    if not run_id:
        return 400, {"error": "run_id is required"}
    status, payload = api_get(run_id)
    if status != 200:
        return status, payload
    event, error = replay_event(run_id, payload.get("steps") or [])
    if error:
        return 409, {"error": error}
    (queue or _queue()).send_message(
        QueueUrl=os.environ["EVENT_QUEUE_URL"],
        MessageBody=json.dumps(event),
    )
    workflow_id = (payload.get("run") or {}).get("workflow_id") or run_id.split(":", 1)[0]
    return 202, {
        "accepted": True,
        "replayed_from": run_id,
        "event_id": event["id"],
        "run_id": f"{workflow_id}:{event['id']}",
    }
