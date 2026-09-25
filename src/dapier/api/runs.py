"""Run history: one run per workflow handling of a trigger event, with the
per-step flow (status, input, output, duration, error) between elements."""
import os

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
    """Roll a run's steps up to one list row: worst status wins.

    A ``filtered`` step finished the run on purpose (a filter stopped the
    chain), so it ranks as its own outcome between processing and completed.
    """
    statuses = [item.get("status", "") for item in items]
    if any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status == "processing" for status in statuses):
        status = "processing"
    elif any(status == "filtered" for status in statuses):
        status = "filtered"
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
    age out of the list but stay reachable through api_get.
    """
    items = _table().scan(Limit=max(limit * 6, 150)).get("Items", [])
    grouped = {}
    for item in items:
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
