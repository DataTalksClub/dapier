"""Run history: one run per workflow handling of a trigger event, with the
per-step flow (status, input, output, duration, error) between elements.

Replay re-injects a past run's original trigger event onto the event queue —
the same dispatch path every hook and custom event takes — so the rerun
lands in run history exactly like a normal run, for failed runs (retry) and
successful ones (replay) alike. Bulk replay-failed does the same for the
latest failed runs of one workflow, and replay-from-step skips the early
steps: a synthetic resume envelope (the worker's own park-and-continue
path) re-runs the workflow from a chosen step on, seeded with the recorded
outputs of everything before it.

Content search answers "which run carried X": the ``q`` filter matches the
recorded step input/output/error inside the same bounded scan window the
list already walks.
"""
import base64
import csv
import io
import json
import os
import time
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Attr, Key

from ..auth import visibility
from botocore.exceptions import ClientError

GSI_NAME = "runs-by-run-id"

STEP_FIELDS = (
    "execution_id", "run_id", "workflow_id", "action_id", "action_type",
    "connector", "event_type", "correlation_id", "status", "started_at",
    "finished_at", "occurred_at", "duration_ms", "error", "input", "output",
    "expires_at", "retry_attempt",
)

DEFAULT_LIMIT = 25
MAX_LIMIT = 200

# The bounded CSV export: the list's filters, one call, capped rows — the
# same caps as the audit trail's export.
EXPORT_DEFAULT_ROWS = 1000
EXPORT_MAX_ROWS = 5000

# One row per run; the fields of run_summary, in CSV order.
CSV_COLUMNS = (
    "run_id", "workflow_id", "connector", "event_type", "status",
    "had_skipped", "steps", "attempts", "failed_step", "delayed_until",
    "started_at", "finished_at", "duration_ms", "error",
)

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

# The delayed-run gate on workflow delete walks more pages than a list (a
# parked run can sit up to 90 days back), but still never reads the whole
# ledger: 60 pages of 300 covers the freshest ~18k executions, far past any
# delay young enough to still be parked (the ledger's TTL is 90 days).
MAX_DELAYED_SCAN_PAGES = 60


def delayed_runs(workflow_id, limit=25):
    """One workflow's runs with steps still parked on a delay, oldest first.

    The gate behind workflow delete: a deleted workflow's parked continuation
    would resume into a definition that no longer exists, so a delete refuses
    while any run is parked (the operator cancels or waits them out). A
    server-side filter on ``workflow_id`` + ``status`` keeps the walk cheap;
    each hit reports the wake-up moment the queue recorded.
    """
    table = _table()
    found = {}
    kwargs = {
        "Limit": 300,
        "FilterExpression": Attr("workflow_id").eq(workflow_id) & Attr("status").eq("delayed"),
    }
    for _ in range(MAX_DELAYED_SCAN_PAGES):
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            run_id = run_id_of(item)
            if not run_id or run_id in found:
                continue
            found[run_id] = {
                "run_id": run_id,
                "delayed_until": (item.get("output") or {}).get("resume_at"),
            }
            if len(found) >= limit:
                return sorted(found.values(), key=lambda run: run["run_id"])
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return sorted(found.values(), key=lambda run: run["run_id"])

# Content search serializes each step's recorded data into a match blob;
# this cap keeps one oversized (or truncated-preview) step from dominating
# the work, while leaving plenty for a needle like an order id.
SEARCH_BLOB_CAP = 20000


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
    rollup reads completed again. A ``cancelled`` step means a suspended run
    was deliberately stopped — its workflow went away, or an operator
    cancelled it — and the parked continuation was dropped: nothing resumes.
    """
    statuses = [item.get("status", "") for item in items]
    if any(status == "failed" for status in statuses):
        status = "failed"
    elif any(status == "processing" for status in statuses):
        status = "processing"
    elif any(status == "delayed" for status in statuses):
        status = "delayed"
    elif any(status == "cancelled" for status in statuses):
        status = "cancelled"
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


def _export_window(max_rows):
    """The scanned window for an export: paged scan pulls sized so the row
    cap is plausibly covered.

    Executions group into runs (a run is usually several steps), so the
    execution budget is twice the row cap. The walk still stops at the
    table's end, and a filtered export may clip early — the same
    bounded-window trade the list makes.
    """
    table = _table()
    items = []
    kwargs = {"Limit": 600}
    for _ in range(max(MAX_SCAN_PAGES, -(-max_rows * 2 // 600) + 1)):
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _grouped(items):
    """The scan window's executions keyed by run, in recorded order."""
    grouped = {}
    for item in items:
        if item.get("kind") == "failure-notice":
            continue
        grouped.setdefault(run_id_of(item), []).append(item)
    return grouped


def _group_runs(items):
    return [run_summary(run_id, group) for run_id, group in _grouped(items).items()]


def _search_blob(run_id, items):
    """The lowercase text one run's content search matches against.

    Every step's identifying fields plus its recorded input, output, and
    error — the data the run actually handled — serialized once per list
    call that carries a ``q``. Runs whose steps never recorded data still
    match on the run id itself.
    """
    parts = [str(run_id)]
    for item in items:
        parts.append(json.dumps(
            {field: item.get(field) for field in
             ("workflow_id", "action_id", "connector", "event_type",
              "error", "input", "output")},
            default=str,
        )[:SEARCH_BLOB_CAP])
    return json.dumps(parts, default=str).lower()


def recent(limit=25, workflow_id=None, status=None, since=None, before=None,
           visible=None):
    """The most recent runs, newest first, optionally filtered.

    A bounded scan groups into runs; runs older than the scan window age
    out of the list but stay reachable through api_get. Failure-notice
    bookkeeping items (written by the worker's notification path) never
    group into the list. Filters narrow the scanned window client-side,
    like the grouping itself — a filter matching only runs older than the
    window returns nothing. ``visible`` (G17 auth.visibility, None =
    unrestricted) drops the runs of workflows the caller may not see.
    """
    status_code, payload = api_list(limit, workflow_id=workflow_id, status=status,
                                    since=since, before=before, visible=visible)
    return payload["runs"] if status_code == 200 else []


def api_list(limit=25, workflow_id=None, status=None, since=None, before=None,
             q=None, next_token=None, visible=None):
    """The list response: runs plus a ``paging`` block.

    ``next`` carries the last returned row's sort key; the follow-up call
    passes it back as ``next_token`` and the window starts strictly after
    that row, so filters and paging compose. ``q`` is content search: a
    case-insensitive substring match over each run's recorded step data
    (input, output, error) and ids, so "which run carried order #1234"
    answers from history. The shape is backward compatible — ``runs`` is
    unchanged, ``paging`` is additive.

    ``visible`` (an auth.visibility.Visibility, None = unrestricted)
    read-filters the list, G17 Phase 2: a non-operator keeps only the runs
    of workflows it owns; a run whose workflow no longer exists resolves to
    no owner and stays visible (the audit duty), as does any run when the
    owner stamp is missing.
    """
    limit = max(1, min(_int(limit) or DEFAULT_LIMIT, MAX_LIMIT))
    token_key = _decode_token(next_token) if next_token else None
    if next_token and token_key is None:
        return 400, {"error": "Invalid page token"}
    query = str(q or "").strip().lower()
    owners = visibility.owners_for(visible)
    grouped = _grouped(_scan_items(limit))
    runs = [run_summary(run_id, group) for run_id, group in grouped.items()]
    runs = [run for run in runs
            if _wanted(run, workflow_id=workflow_id, status=status,
                       since=since, before=before)]
    if visible is not None:
        runs = [run for run in runs
                if visible.workflow_visible(run.get("workflow_id"), owners)]
    if query:
        runs = [run for run in runs
                if query in _search_blob(run["run_id"], grouped[run["run_id"]])]
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
            "filtered": bool(workflow_id or status or since or before
                             or query or next_token),
        },
    }


def runs_to_csv(rows):
    """The run rows as CSV text: one header row, one row per run."""
    def cell(value):
        if value is None:
            return ""
        if value is True:
            return "true"
        if value is False:
            return "false"
        return str(value)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([cell(row.get(column)) for column in CSV_COLUMNS])
    return buffer.getvalue()


def api_export(max_rows=EXPORT_DEFAULT_ROWS, workflow_id=None, status=None,
               since=None, before=None, q=None, now=None, visible=None):
    """The filtered run history as CSV: ``(status, payload)``.

    Same filters as api_list (including content search), newest first,
    capped at ``max_rows`` rows (``truncated`` flags the clip). Returns
    ``{filename, count, truncated, csv}`` — the caller decides delivery
    (CLI file write, console download), like the audit trail's export.
    ``visible`` applies the same G17 read filter as the list, so the CSV
    cannot see past it.
    """
    try:
        max_rows = max(1, min(int(max_rows), EXPORT_MAX_ROWS))
    except (TypeError, ValueError):
        max_rows = EXPORT_DEFAULT_ROWS
    owners = visibility.owners_for(visible)
    grouped = _grouped(_export_window(max_rows))
    rows = [run_summary(run_id, group) for run_id, group in grouped.items()]
    rows = [row for row in rows
            if _wanted(row, workflow_id=workflow_id, status=status,
                       since=since, before=before)]
    if visible is not None:
        rows = [row for row in rows
                if visible.workflow_visible(row.get("workflow_id"), owners)]
    query = str(q or "").strip().lower()
    if query:
        rows = [row for row in rows
                if query in _search_blob(row["run_id"], grouped[row["run_id"]])]
    rows.sort(key=_sort_key, reverse=True)
    truncated = len(rows) > max_rows
    page = rows[:max_rows]
    stamp = datetime.fromtimestamp(int(now if now is not None else time.time()),
                                   timezone.utc)
    return 200, {
        "filename": f"dapier-runs-{stamp:%Y%m%d-%H%M%S}.csv",
        "count": len(page),
        "truncated": truncated,
        "csv": runs_to_csv(page),
    }


def api_get(run_id, visible=None):
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
    owners = visibility.owners_for(visible)
    if visible is not None and not any(
            visible.workflow_visible(item.get("workflow_id"), owners)
            for item in items):
        # Same answer as a missing run: a hidden run must not reveal that
        # it exists (the list the caller came from already dropped it).
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


def _resume_key():
    from ..engine.worker import RESUME_KEY

    return RESUME_KEY


def _workflows_now():
    from ..engine.matching import all_workflows

    return all_workflows()


def _known_step(actions, steps, from_step):
    """Whether ``from_step`` names a real step: recorded in the run, or
    nested anywhere in the workflow's current definition (a branch body, a
    loop body, an error branch)."""
    if any(step.get("action_id") == from_step for step in steps):
        return True

    def walk(chain):
        for action in chain or []:
            if not isinstance(action, dict):
                continue
            if str(action.get("id") or "") == from_step:
                return True
            for value in action.values():
                if isinstance(value, list) and walk(
                        [item for item in value if isinstance(item, dict)]):
                    return True
        return False

    return walk(actions)


def _resume_from_step(run_id, run, steps, from_step, event):
    """The worker resume envelope that re-runs a run from one step on.

    The worker already knows how to continue a parked run: a resume
    envelope carries the remaining chain segments and the accumulated step
    outputs, and the engine replays them through the same step leases as a
    fresh run. Replay-from-step is that path with the pieces rebuilt from
    history: the tail of the workflow's current definition from the chosen
    step onward, every step recorded before it seeded into ``step_outputs``
    (so templates still read ``{steps.<id>.output.*}``), and the fresh
    event the run replay always mints — a new run in history tied to the
    original by correlation id.

    Returns ``(resume_body, None)``, or ``(None, (status, payload))`` when
    the workflow or the step cannot start a replay.
    """
    workflow_id = run.get("workflow_id") or run_id.split(":", 1)[0]
    workflow = next((candidate for candidate in _workflows_now()
                     if candidate.get("id") == workflow_id), None)
    if workflow is None:
        return None, (404, {"error": f"Workflow '{workflow_id}' no longer exists; "
                                     "it cannot be replayed from a step"})
    if not workflow.get("enabled", True):
        return None, (409, {"error": f"Workflow '{workflow_id}' is disabled; "
                                     "enable it before replaying from a step"})
    actions = workflow.get("actions") or []
    position = next((index for index, action in enumerate(actions)
                     if str(action.get("id") or index) == from_step), None)
    if position is None:
        # An id known to the run (or nested in the definition) can name a
        # real step that cannot start a chain — a conflict, not a miss. An
        # id unknown to both is simply not there: 404.
        if not _known_step(actions, steps, from_step):
            return None, (404, {"error": f"Step '{from_step}' is unknown to run "
                                         f"'{run_id}' and workflow '{workflow_id}'"})
        return None, (409, {"error": f"Step '{from_step}' is not a top-level step of "
                                     f"workflow '{workflow_id}'; only top-level "
                                     "steps can start a replay"})
    # Seed every step recorded before the chosen one started (the rerun
    # re-executes the chosen step and everything after); a chosen step the
    # original run never reached seeds everything, the rerun starts fresh
    # there. The same steps ride along as ``reused_steps`` so the resume
    # records them in the rerun's history — they did not run again, the
    # rerun picked up their recorded outputs.
    chosen = next((step for step in steps if step.get("action_id") == from_step), None)
    moment = str(chosen.get("started_at") or "") if chosen else ""
    step_outputs = {}
    reused_steps = []
    for step in steps:
        action_id = str(step.get("action_id") or "")
        if not action_id or (moment and str(step.get("started_at") or "") >= moment):
            continue
        output = step.get("output") if isinstance(step.get("output"), dict) else {}
        entry = {"status": step.get("status") or "completed", "output": output}
        if step.get("error"):
            entry["error"] = step.get("error")
        step_outputs[action_id] = entry
        reused_steps.append({"action_id": action_id,
                             "action_type": step.get("action_type"),
                             "output": output})
    return {
        "workflow_id": workflow_id,
        "event": event,
        "resume_at": time.time(),
        "delay_action_id": None,
        "paused_ids": [],
        "segments": [{"steps": actions[position:], "prefix": "", "scope": None}],
        "step_outputs": step_outputs,
        "reused_steps": reused_steps,
        "run_id": f"{workflow_id}:{event['id']}",
    }, None


def api_replay(run_id, *, queue=None, from_step=None):
    """Re-execute a past run by re-injecting its original trigger event.

    The rebuilt envelope is published to the event queue, so the worker
    picks it up through the normal path: workflows are matched afresh and
    the rerun is recorded in run history like any other run. Asynchronous,
    hence 202; the response projects the replayed run's id from the
    original run's workflow.

    With ``from_step`` (a top-level step id), the early steps do not run
    again — the envelope is a synthetic resume (the worker's own
    park-and-continue path) carrying the workflow from that step onward
    plus the recorded outputs of everything before it, so a long chain can
    be retried at the step that failed without re-firing the trigger and
    the paid-for early actions.
    """
    run_id = str(run_id or "").strip()
    from_step = str(from_step or "").strip()
    if not run_id:
        return 400, {"error": "run_id is required"}
    status, payload = api_get(run_id)
    if status != 200:
        return status, payload
    steps = payload.get("steps") or []
    event, error = replay_event(run_id, steps)
    if error:
        return 409, {"error": error}
    if from_step:
        resume, error = _resume_from_step(run_id, payload.get("run") or {},
                                          steps, from_step, event)
        if error:
            return error
        message = {_resume_key(): resume}
    else:
        message = event
    (queue or _queue()).send_message(
        QueueUrl=os.environ["EVENT_QUEUE_URL"],
        MessageBody=json.dumps(message, default=str),
    )
    workflow_id = (payload.get("run") or {}).get("workflow_id") or run_id.split(":", 1)[0]
    response = {
        "accepted": True,
        "replayed_from": run_id,
        "event_id": event["id"],
        "run_id": f"{workflow_id}:{event['id']}",
    }
    if from_step:
        response["from_step"] = from_step
    return 202, response


def api_cancel(run_id):
    """Cancel a suspended run: its parked steps close out ``cancelled``.

    The durable record of a suspension is the run history itself — a parked
    run's delay steps read ``delayed`` until the queue resumes it — so
    cancelling means flipping every still-``delayed`` step of the run to
    ``cancelled``. Each write is conditional on the step still being parked
    (the same conditional-update style the worker's close-out uses), so a
    resume landing at the same moment cannot double-write a step: exactly
    one side wins, and the worker drops the envelope when it finds the pause
    cancelled. The response carries the rolled-up run after the flip; a run
    with nothing parked is not suspended, hence the 409.
    """
    run_id = str(run_id or "").strip()
    if not run_id:
        return 400, {"error": "run_id is required"}
    status, payload = api_get(run_id)
    if status != 200:
        return status, payload
    parked = [step for step in payload.get("steps") or []
              if step.get("status") == "delayed"]
    if not parked:
        return 409, {"error": "Run is not suspended; nothing to cancel"}
    table = _table()
    cancelled = 0
    for step in parked:
        try:
            table.update_item(
                Key={"execution_id": step.get("execution_id")},
                UpdateExpression=("SET #status = :cancelled, finished_at = :finished, "
                                  "expires_at = :expires"),
                ConditionExpression="#status = :delayed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":cancelled": "cancelled",
                    ":delayed": "delayed",
                    ":finished": datetime.now(timezone.utc).isoformat(),
                    ":expires": int(datetime.now(timezone.utc).timestamp()) + 90 * 86400,
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            continue  # a resume (or a second cancel) already settled the step
        cancelled += 1
    _, refreshed = api_get(run_id)
    return 200, {
        "accepted": True,
        "run_id": run_id,
        "cancelled": cancelled,
        "run": refreshed.get("run") or {},
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


def _workflow_poll_name(workflow_id, connector):
    """The stored poll trigger bound to this workflow (its ``flow:`` key)
    that serves the workflow's own connector — the name the connector's
    live sample branch keys on (every provider sample fetch treats
    ``event`` as the stored poll name). Generic HTTP polls qualify only
    for the generic ``poll`` connector. None when the workflow has no
    matching stored poll, so the sample falls through to
    history/synthetic unchanged."""
    from ..triggers import poll_sources, poll_triggers

    try:
        items = poll_triggers.load_items()
    except Exception:
        return None
    for item in items or []:
        if str(item.get("flow") or "").strip() != workflow_id:
            continue
        try:
            source = poll_sources.stored_source(item)
        except Exception:
            source = None
        if source is None:
            if connector == "poll":
                return str(item.get("poll_id") or "")
            continue
        if source.connector == connector:
            return str(item.get("poll_id") or "")
    return None


def api_trigger_sample(workflow_id, visible=None):
    """The trigger input an author can fill ``{trigger.*}`` templates from.

    The most recent run for ``workflow`` carries its recorded trigger input
    (the same envelope rebuild the replay button uses), which is exactly
    what autofill should offer — the workflow's real last event, not an
    example. With no runs at all, the fall back is the trigger-discovery
    sample for the workflow's own connector (live fetch, then the newest
    recorded run of that connector, then a documented example), so a
    never-run workflow still gets realistic shapes. A workflow driven by a
    stored poll trigger names that poll to the live branch, so the sample
    comes off the real bucket/sheet/channel rather than the documented
    example. Neither source applies — unknown workflow, or a connector
    nothing can sample — is a 404.

    ``visible`` (G17 auth.visibility, None = unrestricted) scopes the whole
    ask: a workflow the caller may not see has no sample (404), and the
    history branch reads only the runs that scope allows.

    Behind ``GET /api/admin|agent/triggers/sample?workflow=<id>``.
    """
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return 400, {"error": "workflow is required"}
    owners = visibility.owners_for(visible)
    if visible is not None and not visible.workflow_visible(workflow_id, owners):
        return 404, {"error": f"no runs recorded for workflow '{workflow_id}'"}
    status, payload = api_list(DEFAULT_LIMIT, workflow_id=workflow_id,
                               visible=visible)
    newest = next(iter(payload.get("runs") or []), None)
    if newest:
        got_status, detail = api_get(newest["run_id"], visible=visible)
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
        discovered = trigger_discovery.discover(
            connector, kind="sample",
            event=_workflow_poll_name(workflow_id, connector)
            or trigger.get("event") or None)
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
