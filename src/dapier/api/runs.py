"""Run history: one run per workflow handling of a trigger event, with the
per-step flow (status, input, output, duration, error) between elements.

Replay re-injects a past run's original trigger event onto the event queue —
the same dispatch path every hook and custom event takes — so the rerun
lands in run history exactly like a normal run, for failed runs (retry) and
successful ones (replay) alike. Bulk replay-failed does the same for the
latest failed runs of one workflow.
"""
import base64
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
    "expires_at", "retry_attempt",
)

DEFAULT_LIMIT = 25
MAX_LIMIT = 200

# The bulk replay-failed cap: one call re-runs at most the latest 50 failed
# runs of the workflow, so a poison workflow cannot fan out unbounded work.
MAX_REPLAY_FAILED = 50

# Friendly status names over the stored run statuses (worst-step rollup in
# run_summary): success covers deliberate early exits, problems covers every
# failure shape. Any other value matches a run status exactly.
STATUS_ALIASES = {
    "success": ("completed", "filtered"),
    "problems": ("failed", "error"),
}

# DynamoDB can't filter grouped runs server-side, so the list walks scan
# pages (never a whole table): page size and page count are both bounded.
MAX_SCAN_PAGES = 10


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
    A ``delayed`` step means the run is parked on a long wait — the queue
    resumes it at the recorded moment (engine.worker) — and ranks the same
    way; once the resume has closed the paused steps out ``completed``, the
    rollup reads completed again.
    """
    statuses = [item.get("status", "") for item in items]
    if any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status == "processing" for status in statuses):
        status = "processing"
    elif any(status == "delayed" for status in statuses):
        status = "delayed"
    elif any(status == "filtered" for status in statuses):
        status = "filtered"
    else:
        status = "completed"
    started = min(str(item.get("started_at") or "") for item in items)
    finished = max(str(item.get("finished_at") or "") for item in items)
    first = min(items, key=lambda item: str(item.get("started_at") or ""))
    failed = next((item for item in items if item.get("status") == "failed"), None)
    delayed = next((item for item in items if item.get("status") == "delayed"), None)
    errors = [item.get("error") for item in items if item.get("error")]
    return {
        "run_id": run_id,
        "workflow_id": first.get("workflow_id") or run_id.split(":")[0],
        "connector": first.get("connector"),
        "event_type": first.get("event_type"),
        "status": status,
        # A step absorbed by ``on_fail: continue`` reads ``skipped``: the run
        # still rolls up completed (the failure was absorbed), so the flag —
        # additive, no new run status — is how a list row surfaces the skip.
        "had_skipped": any(value == "skipped" for value in statuses),
        "steps": len(items),
        "attempts": _max_attempts(items),
        "failed_step": failed.get("action_id") if failed else None,
        # The parked run's wake-up moment, from the delay step's output.
        "delayed_until": ((delayed.get("output") or {}).get("resume_at")
                          if delayed else None),
        "started_at": started or None,
        "finished_at": finished or None,
        "duration_ms": _sum_duration(items),
        "error": errors[0] if errors else None,
    }


def _sum_duration(items):
    total = sum(_int(item.get("duration_ms")) or 0 for item in items)
    return total or None


def _max_attempts(items):
    """The highest recorded attempt across a run's steps (None = first pass)."""
    attempts = [_int(item.get("retry_attempt")) for item in items]
    return max([attempt for attempt in attempts if attempt], default=None)


def _step_view(item):
    view = {field: item.get(field) for field in STEP_FIELDS}
    view["duration_ms"] = _int(item.get("duration_ms"))
    view["expires_at"] = _int(item.get("expires_at"))
    view["retry_attempt"] = _int(item.get("retry_attempt"))
    return view


def _parse_ts(value):
    """An aware UTC datetime for an ISO date/datetime, else None.

    Accepts date-only ("2026-09-25") and full timestamps; a trailing Z and
    a missing offset are read as UTC. Unparseable input returns None so the
    caller can fall back to string comparison.
    """
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _sort_key(run):
    """Newest-first ordering with a run-id tiebreak, so paging is stable."""
    return (run.get("started_at") or "", run.get("run_id") or "")


def _encode_token(run):
    """Opaque stateless page token: the last row's sort key."""
    raw = json.dumps({"s": run.get("started_at") or "", "r": run.get("run_id") or ""},
                     sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_token(token):
    """The ``(started_at, run_id)`` sort key behind a page token, or None."""
    text = str(token or "")
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        key = (str(data.get("s") or ""), str(data.get("r") or ""))
    except (ValueError, TypeError):
        return None
    return key or None


def _out_of_window(started, since=None, before=None):
    """Whether a run start falls outside ``[since, before)``.

    since is inclusive, before exclusive. Both sides parse as timestamps
    when possible; anything unparseable compares as written — every stored
    timestamp shares one UTC offset, so lexicographic order still holds.
    """
    if not since and not before:
        return False
    moment, since_at, before_at = _parse_ts(started), _parse_ts(since), _parse_ts(before)
    if moment is None or (since_at is None and since) or (before_at is None and before):
        if since and started < since:
            return True
        if before and started >= before:
            return True
        return False
    if since_at and moment < since_at:
        return True
    return bool(before_at and moment >= before_at)


def _wanted(run, workflow_id=None, status=None, since=None, before=None):
    """Whether one run summary passes the list filters."""
    if workflow_id and run.get("workflow_id") != workflow_id:
        return False
    if status:
        if run.get("status") not in STATUS_ALIASES.get(status, (status,)):
            return False
    return not _out_of_window(run.get("started_at") or "", since=since, before=before)


def _scan_items(limit):
    """The scanned window for a list call: paged scan pulls.

    Each pull is bounded and the walk stops at the table's end or
    MAX_SCAN_PAGES, whichever comes first — enough to fill a filtered page
    without ever reading the whole ledger.
    """
    table = _table()
    items = []
    kwargs = {"Limit": min(max(limit * 6, 150), 600)}
    for _ in range(MAX_SCAN_PAGES):
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _group_runs(items):
    grouped = {}
    for item in items:
        if item.get("kind") == "failure-notice":
            continue
        grouped.setdefault(run_id_of(item), []).append(item)
    return [run_summary(run_id, group) for run_id, group in grouped.items()]


def recent(limit=25, workflow_id=None, status=None, since=None, before=None):
    """The most recent runs, newest first, optionally filtered.

    A bounded scan groups into runs; runs older than the scan window age
    out of the list but stay reachable through api_get. Failure-notice
    bookkeeping items (written by the worker's notification path) never
    group into the list. Filters narrow the scanned window client-side,
    like the grouping itself — a filter matching only runs older than the
    window returns nothing.
    """
    status_code, payload = api_list(limit, workflow_id=workflow_id, status=status,
                                    since=since, before=before)
    return payload["runs"] if status_code == 200 else []


def api_list(limit=25, workflow_id=None, status=None, since=None, before=None,
             next_token=None):
    """The list response: runs plus a ``paging`` block.

    ``next`` carries the last returned row's sort key; the follow-up call
    passes it back as ``next_token`` and the window starts strictly after
    that row, so filters and paging compose. The shape is backward
    compatible — ``runs`` is unchanged, ``paging`` is additive.
    """
    limit = max(1, min(_int(limit) or DEFAULT_LIMIT, MAX_LIMIT))
    token_key = _decode_token(next_token) if next_token else None
    if next_token and token_key is None:
        return 400, {"error": "Invalid page token"}
    runs = _group_runs(_scan_items(limit))
    runs = [run for run in runs
            if _wanted(run, workflow_id=workflow_id, status=status,
                       since=since, before=before)]
    runs.sort(key=_sort_key, reverse=True)
    if token_key is not None:
        runs = [run for run in runs if _sort_key(run) < token_key]
    page = runs[:limit]
    more = len(runs) > limit
    return 200, {
        "runs": page,
        "paging": {
            "next": _encode_token(page[-1]) if more and page else None,
            "limit": limit,
            "filtered": bool(workflow_id or status or since or before or next_token),
        },
    }


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


def api_replay_failed(workflow_id, *, queue=None):
    """Replay the latest failed runs of one workflow, Zapier-style.

    Enumerates the workflow's failed runs through the same filtered list the
    operator sees (bounded to MAX_REPLAY_FAILED) and re-injects each through
    api_replay. Runs whose event data was never recorded, or only as a
    truncated preview, are skipped with a reason instead of failing the
    batch. Asynchronous like a single replay, hence 202.
    """
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return 400, {"error": "workflow_id is required"}
    status, payload = api_list(MAX_REPLAY_FAILED, workflow_id=workflow_id, status="failed")
    if status != 200:
        return status, payload
    results = []
    replayed = 0
    for run in payload["runs"]:
        run_status, run_payload = api_replay(run["run_id"], queue=queue)
        if run_status == 202:
            replayed += 1
            results.append({
                "run_id": run["run_id"],
                "replayed": True,
                "new_run_id": run_payload.get("run_id"),
            })
        else:
            results.append({
                "run_id": run["run_id"],
                "replayed": False,
                "reason": run_payload.get("error") or f"replay returned {run_status}",
            })
    return 202, {
        "accepted": True,
        "workflow_id": workflow_id,
        "replayed": replayed,
        "skipped": len(results) - replayed,
        "runs": results,
    }


def api_trigger_sample(workflow_id):
    """The trigger input an author can fill ``{trigger.*}`` templates from.

    The most recent run for ``workflow`` carries its recorded trigger input
    (the same envelope rebuild the replay button uses), which is exactly
    what autofill should offer — the workflow's real last event, not an
    example. With no runs at all, the fall back is the trigger-discovery
    sample for the workflow's own connector (live fetch, then the newest
    recorded run of that connector, then a documented example), so a
    never-run workflow still gets realistic shapes. Neither source applies
    — unknown workflow, or a connector nothing can sample — is a 404.

    Behind ``GET /api/admin|agent/triggers/sample?workflow=<id>``.
    """
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return 400, {"error": "workflow is required"}
    status, payload = api_list(DEFAULT_LIMIT, workflow_id=workflow_id)
    newest = next(iter(payload.get("runs") or []), None)
    if newest:
        got_status, detail = api_get(newest["run_id"])
        if got_status == 200:
            envelope, error = replay_event(newest["run_id"], detail.get("steps") or [])
            if envelope is not None and not error:
                return 200, {
                    "workflow": workflow_id,
                    "source": "history",
                    "run_id": newest["run_id"],
                    "connector": envelope.get("connector"),
                    "event": envelope.get("event"),
                    "occurred_at": newest.get("started_at"),
                    "data": envelope.get("data") if isinstance(envelope.get("data"), dict) else {},
                }
    from . import overview

    trigger = next(
        (workflow.get("trigger") or {} for workflow in overview._workflows()
         if workflow.get("id") == workflow_id),
        {},
    )
    connector = str(trigger.get("connector") or "").strip()
    if not connector:
        return 404, {"error": f"no runs recorded for workflow '{workflow_id}'"}
    from ..connectors import trigger_discovery

    try:
        discovered = trigger_discovery.discover(connector, kind="sample",
                                                event=trigger.get("event") or None)
    except trigger_discovery.DiscoveryNotFound as exc:
        return 404, {"error": str(exc)}
    except trigger_discovery.DiscoveryUpstream as exc:
        return 502, {"error": str(exc)}
    except ValueError as exc:
        return 400, {"error": str(exc)}
    sample = discovered.get("sample") or {}
    return 200, {
        "workflow": workflow_id,
        "source": discovered.get("source") or "discovery",
        "connector": discovered.get("connector"),
        "event": discovered.get("event"),
        "occurred_at": sample.get("occurred_at"),
        "data": sample.get("data") if isinstance(sample.get("data"), dict) else {},
    }
