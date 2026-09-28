import json
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone

from ..connectors.ingress import normalize_event
from ..triggers import inbox
from . import _run_connector, usage
from .logic import RunSuspended, resume_chain, run_chain
from .matching import all_workflows, matches
from .notify import notify_failure


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Parked runs: the envelope a suspended run leaves on the event queue
# (engine.logic's RunSuspended note has the scheme). The worker detects it
# before any trigger-event handling: it is not an event, it is a run.
RESUME_KEY = "dapier_resume"

# SQS DelaySeconds caps one parking hop at 15 minutes; a wait longer than
# that chains: every resume that lands before ``resume_at`` re-enqueues the
# continuation for the remainder (900 + 900 + ... until the moment has
# passed and the captured remainder replays).
MAX_DELAY_QUEUE_SECONDS = 900

# A workflow opts into retries with a top-level ``retry:`` mapping; attempts
# is the total try count (1 = a single attempt, the default) and
# backoff_seconds the SQS delay between tries. Values outside the bounds clamp.
RETRY_ATTEMPT_BOUNDS = (1, 5)
RETRY_BACKOFF_BOUNDS = (1, 900)
RETRY_DEFAULTS = {"attempts": 1, "backoff_seconds": 60}

# Steps the engine runs itself (engine.logic) rather than dispatching to a
# connector runner; their failures are configuration problems a repeat cannot
# fix, so they fall through to the failure path at once.
LOGIC_STEP_TYPES = frozenset({"filter", "condition", "delay", "paths", "for_each"})


def retry_policy(workflow):
    """The workflow's retry policy, clamped; None when it does not opt in.

    Without the key (or with a non-mapping value) a failed run behaves
    exactly as before: one attempt, then the failure path. Steps that handle
    their own failure (``on_fail``/``on_error``) never reach this policy —
    their author already decided what a failure means.
    """
    retry = (workflow or {}).get("retry")
    if not isinstance(retry, dict):
        return None
    return {
        "attempts": _clamp(retry.get("attempts", RETRY_DEFAULTS["attempts"]),
                           *RETRY_ATTEMPT_BOUNDS),
        "backoff_seconds": _clamp(retry.get("backoff_seconds", RETRY_DEFAULTS["backoff_seconds"]),
                                  *RETRY_BACKOFF_BOUNDS),
    }


def _clamp(value, low, high):
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError):
        return low


def _message_attempt(record):
    """The attempt counter this SQS message carries (0 = first pass).

    The counter travels as a message attribute so it never pollutes the
    event body: matching and replay read the body as pure trigger data.
    """
    try:
        return int(record["messageAttributes"]["retry_attempt"]["stringValue"])
    except (KeyError, TypeError, ValueError):
        return 0


class LeaseBusy(Exception):
    """A redelivery hit a step whose processing lease is still live.

    Raised instead of silently skipping the step: the record goes back on
    the queue (batchItemFailure, no failure notice) and the next attempt
    either finds the step finished (dedupe: skip) or the lease expired
    (the first holder died; take over and run).
    """


def normalize_payload(payload):
    """SES envelope unwrap, then per-connector normalization
    (src/dapier/connectors/ingress.py)."""
    if payload.get("Type") == "Notification" and isinstance(payload.get("Message"), str):
        payload = json.loads(payload["Message"])
    return normalize_event(payload)


def _execution_id(workflow_id, action_id, event):
    return f"{workflow_id}:{action_id}:{event['id']}"


def _run_id(workflow_id, event):
    """One run = one workflow's handling of one trigger event."""
    return f"{workflow_id}:{event.get('id', '')}"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


# The trigger envelope every step records as its ``input``: capped higher
# than step outputs so an ordinary webhook or email event (hook bodies run
# to 200 KB, email bodies to 60 KB apiece) still replays from run history —
# api/runs.replay_event refuses a truncated input. Events too big even for
# this keep that explicit refusal.
TRIGGER_INPUT_LIMIT = 65_000


def _trim(value, limit=6000):
    """Cap a captured step value so execution items stay far below the
    DynamoDB 400 KB limit; oversized values keep a JSON preview."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value))
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


def _is_pending(workflow_id, action_id, event, action_type=None, retry_attempt=None):
    import boto3
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"])
    now = int(time.time())
    item = {
        "execution_id": _execution_id(workflow_id, action_id, event),
        "run_id": _run_id(workflow_id, event),
        "workflow_id": workflow_id,
        "action_id": action_id,
        "connector": event.get("connector"),
        "event_type": event.get("event"),
        "correlation_id": event.get("correlation_id") or event.get("id"),
        "status": "processing",
        "started_at": _now_iso(),
        "lease_until": now + 300,
        "expires_at": now + 90 * 86400,
        # Step telemetry for the run view: the action type and the event data
        # every action in the flow receives as its input. The input is the
        # trigger envelope, so it gets the replay-sized cap (run-history
        # replay re-injects it); outputs keep the small step cap.
        "input": _trim(event.get("data") or {}, limit=TRIGGER_INPUT_LIMIT),
    }
    if action_type:
        item["action_type"] = action_type
    if retry_attempt:
        # Run history shows the retry: this step record belongs to a later
        # attempt of the same event.
        item["retry_attempt"] = int(retry_attempt)
    if event.get("occurred_at"):
        item["occurred_at"] = event["occurred_at"]
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(execution_id) OR lease_until < :now",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        # Another delivery of this event holds the step's lease. A step that
        # already finished is plain dedupe — skip it. One still processing
        # must not be passed over (the chain would run downstream steps on
        # missing outputs): raise so the record is retried until the lease
        # decides — holder finished → skip, holder died → lease expires and
        # this attempt takes over.
        existing = table.get_item(Key={"execution_id": item["execution_id"]}).get("Item") or {}
        if existing.get("status") in ("completed", "filtered"):
            return False
        raise LeaseBusy(
            f"step '{action_id}' of {workflow_id} is still processing a previous "
            "delivery of this event; the record will be retried"
        ) from None
    return True


def _mark_completed(workflow_id, action_id, event, output=None, duration_ms=None,
                    status="completed"):
    """Close out a step; ``status`` is ``completed`` or ``filtered`` (a filter
    stopped the chain — quiet, but visible in run history). A step that failed
    under ``on_error: continue|run`` also closes out here, as ``failed``: the
    step is handled, so the run goes on instead of failing."""
    import boto3

    sets = ["#status = :status", "finished_at = :finished", "expires_at = :expires"]
    names = {"#status": "status"}
    values = {
        ":status": status,
        ":finished": _now_iso(),
        ":expires": int(time.time()) + 90 * 86400,
    }
    if output is not None:
        sets.append("#output = :output")
        names["#output"] = "output"
        values[":output"] = _trim(output)
    if duration_ms is not None:
        sets.append("duration_ms = :duration")
        values[":duration"] = int(duration_ms)
    boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).update_item(
        Key={"execution_id": _execution_id(workflow_id, action_id, event)},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    if status == "completed" and os.environ.get("TASK_USAGE_TABLE"):
        try:
            usage.add_task(workflow_id)
        except Exception:
            # Usage is a metric, not a step outcome — a rollup failure must
            # never fail the run that produced the task.
            logger.info("task usage rollup failed", exc_info=True)


def _release_action(workflow_id, action_id, event, exc=None, duration_ms=None):
    import boto3

    now = int(time.time())
    message = (str(exc) or exc.__class__.__name__) if exc is not None else "Action failed"
    sets = [
        "#status = :failed", "finished_at = :finished", "#error = :error",
        "lease_until = :lease", "expires_at = :expires",
    ]
    names = {"#status": "status", "#error": "error"}
    values = {
        ":failed": "failed",
        ":finished": _now_iso(),
        ":error": message[:500],
        ":lease": now - 1,
        ":expires": now + 90 * 86400,
    }
    if duration_ms is not None:
        sets.append("duration_ms = :duration")
        values[":duration"] = int(duration_ms)
    boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).update_item(
        Key={"execution_id": _execution_id(workflow_id, action_id, event)},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def execute(event, before_action=None, after_action=None, on_action_error=None):
    """Match and run every workflow the event picks up — the worker's path.

    Same matching, same hooks, same dispatch as ``engine.execute`` (the
    test-run path, which must never park), plus the one thing the shared
    engine cannot do: a delay step past the inline sleep cap raises
    ``RunSuspended``, and the suspension only knows the chain it unwound
    from — here it is tagged with the workflow and event it parked, so the
    handler can leave the run's continuation on the queue (``_park_suspension``).
    The raise happens only after the sweep: every other matching workflow
    still runs, and extra suspensions park inline — one workflow's long
    delay must not silence its siblings (the handler completes the record,
    so nothing would re-deliver them). Matching, hooks and dispatch stay in
    lockstep with engine.execute.
    """
    matched = []
    suspended = []
    for workflow in all_workflows():
        if matches(workflow, event):
            matched.append(workflow["id"])
            try:
                run_chain(
                    workflow["id"], workflow.get("actions", []), event, _run_connector,
                    before_action=before_action,
                    after_action=after_action,
                    on_action_error=on_action_error,
                )
            except RunSuspended as susp:
                susp.workflow_id = workflow["id"]
                susp.event = event
                suspended.append(susp)
    if suspended:
        # The handler parks the one raised suspension and completes the
        # record — which would strand every other workflow this event
        # matched (no redelivery follows a completed record). So each
        # suspension beyond the first parks right here, and only the first
        # propagates. A failed inline park escapes past RunSuspended: the
        # record fails, the event re-runs, and leases skip the steps that
        # already ran — the same recovery the handler's park path relies on.
        for susp in suspended[1:]:
            _park_suspension(susp)
        raise suspended[0]
    return matched


def _sqs():
    import boto3

    return boto3.client("sqs")


def _enqueue_resume(workflow_id, event, resume_at, segments, step_outputs,
                    delay_action_id=None, *, paused_ids=None, queue=None):
    """Leave one parked run's continuation on the event queue.

    The parked delay step itself never re-runs: logic closed it out
    ``delayed`` through the after hook (run history shows the pause) and
    excluded it from the resume segments. ``paused_ids`` is every step the
    unwind closed out ``delayed`` (the delay itself, innermost, plus any
    branch/loop wrappers) — the resume closes them ``completed`` again once
    the run gets past the pause. The envelope carries the absolute
    ``resume_at`` — not a countdown — so chained hops stay exact no matter
    when each one lands; ``DelaySeconds`` is the remaining wait, capped at
    ``MAX_DELAY_QUEUE_SECONDS``.
    """
    remaining = max(float(resume_at) - time.time(), 0.0)
    chunk = int(min(math.ceil(remaining), MAX_DELAY_QUEUE_SECONDS))
    body = json.dumps({
        RESUME_KEY: {
            "workflow_id": workflow_id,
            "event": event,
            "resume_at": float(resume_at),
            "delay_action_id": delay_action_id,
            "paused_ids": paused_ids or (
                [delay_action_id] if delay_action_id else []),
            "segments": segments,
            "step_outputs": step_outputs or {},
            "run_id": _run_id(workflow_id, event or {}),
        }
    }, default=str)
    (queue if queue is not None else _sqs()).send_message(
        QueueUrl=os.environ["EVENT_QUEUE_URL"],
        MessageBody=body,
        DelaySeconds=chunk,
    )
    return chunk


def _park_suspension(susp, *, queue=None):
    """Park a run that hit a delay past the inline sleep cap.

    Not a failure: the suspension already closed the delay step out through
    the after hook, so parking is one enqueue of the continuation envelope.
    """
    return _enqueue_resume(
        susp.workflow_id, susp.event, susp.resume_at,
        susp.segments, susp.step_outputs, susp.delay_action_id,
        paused_ids=susp.paused_ids, queue=queue,
    )


def _run_steps(run_id):
    """The run's step records, read the way the runs API reads them: one GSI
    query over the same index api/runs.py groups runs by."""
    from boto3.dynamodb.conditions import Key

    import boto3

    return boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).query(
        IndexName="runs-by-run-id",
        KeyConditionExpression=Key("run_id").eq(str(run_id)),
    ).get("Items", [])


def _cancel_paused_step(workflow_id, action_id, event):
    """A suspended run that must not continue — its workflow was unpublished,
    disabled, or deleted, or an operator cancelled it — closes its parked
    steps out ``cancelled`` instead of ``completed``, so run history shows a
    deliberate stop, not a pause. The write is conditional on the step still
    being ``delayed``, so a resume landing on one side and an operator cancel
    on the other cannot double-write a step: exactly one of them wins."""
    if os.environ.get("EXECUTIONS_TABLE"):
        import boto3
        from botocore.exceptions import ClientError

        try:
            boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).update_item(
                Key={"execution_id": _execution_id(workflow_id, action_id, event)},
                UpdateExpression=("SET #status = :status, finished_at = :finished, "
                                  "expires_at = :expires"),
                ConditionExpression="#status = :delayed",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":status": "cancelled",
                    ":delayed": "delayed",
                    ":finished": _now_iso(),
                    ":expires": int(time.time()) + 90 * 86400,
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            # The step is no longer parked (a sibling delivery resumed it, or
            # the cancel came from the other side): nothing left to close out.


def _pause_cancelled(workflow_id, event, run_id, paused):
    """Whether an operator cancelled the parked run while the envelope was in
    flight: one consistent read of the run's step records, and any paused
    step reading ``cancelled`` — api_cancel's mark — means the remainder must
    not run. The still-``delayed`` siblings close out ``cancelled`` too, and
    the caller drops the envelope. Steps the read cannot see (nothing
    recorded under the run yet) keep the replay as today: the check only ever
    stops a run it can see was cancelled."""
    if not run_id or not paused or not os.environ.get("EXECUTIONS_TABLE"):
        return False
    recorded = {str(item.get("action_id") or ""): item.get("status")
                for item in _run_steps(run_id)}
    if not any(recorded.get(action_id) == "cancelled" for action_id in paused):
        return False
    for action_id in paused:
        _cancel_paused_step(workflow_id, action_id, event)
    return True


def _close_paused_step(workflow_id, action_id, event, *, count_usage=False):
    """The resume arrived, so the run got past its pause: a step the unwind
    had closed out ``delayed`` reads ``completed`` again. Same record update
    as ``_mark_completed`` minus the output (the pause summary stays on the
    record); only the delay step itself counts a usage task — the wrappers
    merely carried the pause out of their branches.
    """
    if os.environ.get("EXECUTIONS_TABLE"):
        import boto3

        boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).update_item(
            Key={"execution_id": _execution_id(workflow_id, action_id, event)},
            UpdateExpression=("SET #status = :status, finished_at = :finished, "
                              "expires_at = :expires"),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": "completed",
                ":finished": _now_iso(),
                ":expires": int(time.time()) + 90 * 86400,
            },
        )
    if count_usage and os.environ.get("TASK_USAGE_TABLE"):
        try:
            usage.add_task(workflow_id)
        except Exception:
            logger.info("task usage rollup failed", exc_info=True)


def _mark_reused(workflow_id, event, *, action_id, action_type=None, output=None):
    """Record a replayed-from-step run's earlier step as ``reused``.

    Replay-from-step (api/runs.py) resumes a run through this module's
    resume envelope with the earlier steps' recorded outputs seeded in —
    those steps do not run again, but the rerun's history should still show
    them: without a record the rerun would read as if the chain started at
    the replayed step. One row per reused step, status ``reused`` (a new
    rollup-invisible status: neither success nor failure — the run's own
    outcome comes from the steps that did re-execute), carrying the recorded
    output that was seeded into the ``steps`` context. Only envelopes that
    name ``reused_steps`` write anything: a delay resume's parked steps are
    real records already, never rewritten here.
    """
    if not os.environ.get("EXECUTIONS_TABLE") or not str(action_id or "").strip():
        return
    import boto3

    now = int(time.time())
    item = {
        "execution_id": _execution_id(workflow_id, str(action_id), event),
        "run_id": _run_id(workflow_id, event),
        "workflow_id": workflow_id,
        "action_id": str(action_id),
        "connector": event.get("connector"),
        "event_type": event.get("event"),
        "correlation_id": event.get("correlation_id") or event.get("id"),
        "status": "reused",
        "started_at": _now_iso(),
        "finished_at": _now_iso(),
        "expires_at": now + 90 * 86400,
        "input": _trim(event.get("data") or {}, limit=TRIGGER_INPUT_LIMIT),
    }
    if action_type:
        item["action_type"] = str(action_type)
    if output is not None:
        item["output"] = _trim(output)
    boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).put_item(Item=item)


def _resume_run(resume, *, queue=None):
    """Continue a parked run from its ``dapier_resume`` envelope.

    An arrival can predate ``resume_at`` (the 900s cap chained the wait):
    the envelope goes straight back on the queue for the remainder and
    nothing runs early. Once the moment has passed, the run still answers to
    the workflow's current state — the fresh-event path re-matches on every
    delivery, so the parked continuation checks here instead: a workflow
    that was unpublished, disabled, or deleted cancels the parked steps and
    the envelope is consumed (no replay, no redrive), as does a run an
    operator cancelled in the meantime. Otherwise the paused steps close out
    ``completed`` (the pause is over) and the captured segments replay
    through the same conditional-write step leases as a fresh run, so
    redeliveries of the continuation stay safe; a further delay in the
    remainder suspends again and the handler parks the run once more.
    """
    workflow_id = str(resume.get("workflow_id") or "")
    event = resume.get("event")
    if not workflow_id or not isinstance(event, dict):
        logger.info("resume envelope carries nothing to run; dropping",
                    extra={"run_id": resume.get("run_id")})
        return None
    if float(resume.get("resume_at") or 0.0) > time.time():
        return _enqueue_resume(
            workflow_id, event, resume["resume_at"],
            resume.get("segments") or [], resume.get("step_outputs"),
            resume.get("delay_action_id"),
            paused_ids=resume.get("paused_ids"),
            queue=queue,
        )
    # Envelopes parked before ``paused_ids`` existed carry only
    # ``delay_action_id`` — close that one out rather than leaving a stale
    # ``delayed`` record behind.
    paused = [str(action_id) for action_id in
              (resume.get("paused_ids")
               or ([resume["delay_action_id"]] if resume.get("delay_action_id") else []))
              if action_id]
    workflow = next((candidate for candidate in all_workflows()
                     if candidate.get("id") == workflow_id), None)
    if workflow is None or not workflow.get("enabled", True):
        for action_id in paused:
            _cancel_paused_step(workflow_id, action_id, event)
        logger.info("suspended run dropped: workflow is gone or disabled",
                    extra={"run_id": resume.get("run_id"), "workflow_id": workflow_id})
        return None
    if _pause_cancelled(workflow_id, event,
                        resume.get("run_id") or _run_id(workflow_id, event), paused):
        logger.info("suspended run dropped: cancelled by an operator",
                    extra={"run_id": resume.get("run_id"), "workflow_id": workflow_id})
        return None
    if paused:
        for position, action_id in enumerate(paused):
            _close_paused_step(workflow_id, action_id, event,
                               count_usage=(position == 0))
    # A replay-from-step envelope (api/runs.py) lists the earlier steps it
    # seeded from history: record them as ``reused`` in the rerun's run
    # history before the remainder executes. Written only once the run is
    # actually going ahead — a dropped envelope (workflow gone, operator
    # cancel) records nothing.
    for reused in resume.get("reused_steps") or []:
        if not isinstance(reused, dict):
            continue
        _mark_reused(workflow_id, event, action_id=reused.get("action_id"),
                     action_type=reused.get("action_type"),
                     output=reused.get("output"))
    try:
        return resume_chain(
            workflow_id, resume.get("segments") or [], event, _run_connector,
            before_action=_is_pending, after_action=_mark_completed,
            on_action_error=_release_action,
            step_outputs=resume.get("step_outputs") or {},
        )
    except RunSuspended as susp:
        # A chained wait (or another delay later in the remainder): the
        # unwound segments carry what is left; identify the run for the park.
        susp.workflow_id = workflow_id
        susp.event = event
        raise


def _schedule_event(payload):
    """Normalize a schedule trigger fire into a dapier event.

    The EventBridge target input replaces the whole event, so the payload
    names the trigger and the fire's id and time are minted here.
    """
    schedule_id = payload["schedule_id"]
    fired_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": "1.0",
        "id": f"{schedule_id}-{uuid.uuid4()}",
        "correlation_id": f"{schedule_id}-{fired_at}",
        "connector": "schedule",
        "event": "schedule.triggered",
        "source": schedule_id,
        "occurred_at": fired_at,
        "data": {
            "schedule": schedule_id,
            "utc_time": fired_at,
        },
    }


def _poll_failure_event(event):
    """A minimal envelope naming a failed poll-trigger fire.

    The EventBridge target input is a constant (no per-fire id), so the
    notice id mints a uuid: each failed fire notifies once. The shape is
    what ``notify_failure``'s ingress path reads (connector ``poll`` with
    ``data.poll`` naming the trigger); there is no workflow behind the
    failure, so it resolves to the operator recipient.
    """
    poll_id = event.get("poll_id")
    return {
        "schema_version": "1.0",
        "id": str(uuid.uuid4()),
        "correlation_id": f"poll:{poll_id}",
        "connector": "poll",
        "event": "poll.failed",
        "source": poll_id,
        "data": {"poll": poll_id},
    }


def _attempt_hooks(attempt):
    """Step-telemetry hooks for one attempt of an event.

    Shape-identical to the plain hooks (first pass = attempt 0 = the default
    behavior); a later attempt marks its step records with the attempt number
    so run history shows the retry.
    """

    def before_action(workflow_id, action_id, event, action_type=None):
        return _is_pending(workflow_id, action_id, event, action_type,
                           retry_attempt=attempt or None)

    return {
        "before_action": before_action,
        "after_action": _mark_completed,
        "on_action_error": _release_action,
    }


def _schedule_retry(exc, event, attempt, *, queue=None):
    """Re-enqueue a failed action run when the workflow's retry policy has
    attempts left; True when the retry was scheduled.

    The event goes back on the queue with the policy's delay and the next
    attempt number as a message attribute, so the rerun enters through the
    normal dispatch path (completed steps stay leased out for a while). The
    record is then handled: no batch failure, no failure notification — the
    worker's failure path (notify, redrive) applies only once the attempts
    are exhausted. Only action failures retry: a logic-step failure is a
    configuration problem a repeat cannot fix, and a step that handles its
    own failure (on_fail/on_error) never reaches the worker at all.
    """
    if not isinstance(event, dict) or not event.get("id"):
        return False
    workflow_id = getattr(exc, "dapier_workflow", None)
    if not workflow_id or getattr(exc, "dapier_step_type", "") in LOGIC_STEP_TYPES:
        return False
    workflow = next(
        (item for item in all_workflows() if item.get("id") == workflow_id), None)
    policy = retry_policy(workflow)
    if not policy or attempt + 1 >= policy["attempts"]:
        return False
    next_attempt = attempt + 1
    failed_step = getattr(exc, "dapier_step_id", None)
    if failed_step:
        _annotate_retry(workflow_id, failed_step, event, next_attempt)
    (queue if queue is not None else _sqs()).send_message(
        QueueUrl=os.environ["EVENT_QUEUE_URL"],
        MessageBody=json.dumps(event),
        DelaySeconds=policy["backoff_seconds"],
        MessageAttributes={
            "retry_attempt": {"DataType": "Number", "StringValue": str(next_attempt)},
        },
    )
    logger.info("retry scheduled", extra={
        "workflow_id": workflow_id, "event_id": event.get("id"),
        "attempt": next_attempt, "delay_seconds": policy["backoff_seconds"],
    })
    return True


def _annotate_retry(workflow_id, action_id, event, attempt):
    """Mark the failed step with the attempt the retry will run, so history
    shows the pending retry even before it fires."""
    import boto3

    boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"]).update_item(
        Key={"execution_id": _execution_id(workflow_id, action_id, event)},
        UpdateExpression="SET retry_attempt = :attempt",
        ExpressionAttributeValues={":attempt": int(attempt)},
    )


def handler(event, _context):
    if isinstance(event, dict) and event.get("trigger") == "poll" and event.get("poll_id"):
        # EventBridge invokes the function directly (dapier-poll-* rules):
        # fetch one page, emit each new item as its own event.
        try:
            from ..triggers import poll_triggers

            poll_triggers.fire(event["poll_id"])
        except Exception as exc:
            logger.exception("poll trigger failed", extra={"poll_id": event.get("poll_id")})
            notify_failure(exc, _poll_failure_event(event))
            raise
        return {"executed": event["poll_id"]}
    if isinstance(event, dict) and event.get("trigger") == "schedule" and event.get("schedule_id"):
        # EventBridge invokes the function directly: the target input names
        # the trigger, and the envelope carries the fire's id and time.
        normalized = None
        try:
            normalized = _schedule_event(event)
            execute(
                normalized,
                **_attempt_hooks(0),
            )
        except RunSuspended as susp:
            # The run parked on a long delay; the continuation re-enters
            # through the event queue when the wait is over. Not a failure.
            _park_suspension(susp)
        except Exception as exc:
            logger.exception("schedule trigger failed", extra={"schedule_id": event.get("schedule_id")})
            if not _schedule_retry(exc, normalized, 0):
                notify_failure(exc, normalized)
                raise
        return {"executed": event["schedule_id"]}
    failures = []
    for record in event.get("Records", []):
        payload = None
        inbox_id = None
        attempt = _message_attempt(record)
        try:
            payload = json.loads(record["body"])
            if (isinstance(payload, dict) and "connector" not in payload
                    and isinstance(payload.get(RESUME_KEY), dict)):
                # A parked run's continuation, not a trigger event: no inbox
                # record, no matching — the captured remainder replays as-is.
                _resume_run(payload[RESUME_KEY])
                continue
            payload = normalize_payload(payload)
            inbox_id = inbox.record(payload)
            matched = execute(
                payload,
                **_attempt_hooks(attempt),
            )
        except LeaseBusy as exc:
            # Not a failure: the event is already being handled. Requeue for
            # the lease to decide, quietly.
            logger.info("step lease busy; requeueing record", extra={
                "message_id": record.get("messageId"), "reason": str(exc)})
            inbox.complete(inbox_id, None, error="requeued: a delivery is still in flight")
            failures.append({"itemIdentifier": record.get("messageId")})
        except RunSuspended as susp:
            # The run parked on a long delay. The continuation re-enters
            # through the event queue when the wait is over; a park that
            # cannot enqueue fails the record so the event re-runs (leases
            # dedupe the steps that already ran, the delay suspends again).
            logger.info("run parked on a long delay", extra={
                "message_id": record.get("messageId"), "run_id": susp.workflow_id})
            try:
                _park_suspension(susp)
            except Exception as park_exc:
                logger.exception("parking suspended run failed",
                                 extra={"message_id": record.get("messageId")})
                inbox.complete(inbox_id, None, error=park_exc)
                failures.append({"itemIdentifier": record.get("messageId")})
            else:
                inbox.complete(inbox_id, [susp.workflow_id])
        except Exception as exc:
            logger.exception("workflow record failed", extra={"message_id": record.get("messageId")})
            if _schedule_retry(exc, payload, attempt):
                # The retry owns the record now: no batch failure, no failure
                # notice. The inbox row notes the pending attempt; the retry's
                # own close-out rewrites it (matched, or the final failure).
                inbox.complete(inbox_id, None, error="retry scheduled: a later "
                               "attempt is pending on the event queue")
                continue
            inbox.complete(inbox_id, None, error=exc)
            notify_failure(exc, payload)
            failures.append({"itemIdentifier": record.get("messageId")})
        else:
            inbox.complete(inbox_id, matched)
    return {"batchItemFailures": failures}
