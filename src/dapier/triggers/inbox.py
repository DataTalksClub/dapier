"""Trigger inbox: every inbound trigger event, matched or not.

Zapier's trigger inbox shows each incoming event — including the ones no zap
claimed yet — so an author can confirm data is arriving and send it through
on demand. Dapier's run history only ever sees events a workflow matched; an
event delivered before its trigger was wired to a flow (or while a filter
typo rejected it) left no trace outside CloudWatch.

The worker records every queue-delivered event here when it picks it up
(:func:`record`, deduplicated by event id) and closes the row out after the
run (:func:`complete`) with the workflows that matched, so unmatched events
stay visible and replayable. Replay re-injects the stored envelope onto the
event queue exactly like the run-history replay button — a fresh event id
(fresh run in history) tied to the original via ``correlation_id``.

Best-effort by design: an inbox write failure must never fail a trigger
delivery, so :func:`record` and :func:`complete` swallow storage errors
(logged) and no-op when ``TRIGGER_INBOX_TABLE`` is not configured. The
read/replay API raises :class:`InboxError` instead, so operators get a real
message rather than an empty list.
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TABLE_ENV = "TRIGGER_INBOX_TABLE"
RETENTION_DAYS = 30
DEFAULT_LIMIT = 25
MAX_LIMIT = 100

# A row the worker has not closed out yet; the run is in flight or the
# worker died mid-run (the retry heals the row via complete()).
RECEIVED = "received"
MATCHED = "matched"
UNMATCHED = "unmatched"
FAILED = "failed"

# Terminal step statuses, mirrored from engine.worker — a redelivery may
# skip a step that already finished, never one still being processed.
_DONE_STATUSES = ("completed", "filtered")


class InboxError(Exception):
    """The inbox is not configured, or the event cannot be replayed."""


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _trim(value, limit=6000):
    """Cap stored data so inbox items stay far below the 400 KB item limit."""
    try:
        text = json.dumps(value, default=str)
    except (TypeError, ValueError):
        text = json.dumps(str(value))
    if len(text) <= limit:
        return value
    return {"truncated": True, "preview": text[:limit]}


def _table():
    name = os.environ.get(TABLE_ENV)
    if not name:
        raise InboxError("the trigger inbox is not configured")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _decode_numbers(value):
    """DynamoDB's resource API returns Decimals; API payloads stay JSON-safe."""
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _decode_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_numbers(item) for item in value]
    return value


def record(event, *, table_ref=None):
    """Record an inbound event before the engine runs it.

    Deduplicated by the event id: SQS redeliveries and duplicate webhook
    pushes update nothing (the close-out update after the run is the idempotent
    path). Returns the inbox id, or None when the inbox is not configured, the
    event has no id, or the write failed — callers must treat None as "not
    recorded" and move on. The row starts as ``received``; :func:`complete`
    sets the outcome.
    """
    if not isinstance(event, dict) or not event.get("id"):
        return None
    now = int(datetime.now(timezone.utc).timestamp())
    item = {
        "inbox_id": str(event["id"]),
        "connector": event.get("connector"),
        "event": event.get("event"),
        "source": event.get("source"),
        "occurred_at": event.get("occurred_at"),
        "received_at": _now_iso(),
        "status": RECEIVED,
        "data": _trim(event.get("data") or {}),
        "matched": [],
        "expires_at": now + RETENTION_DAYS * 86400,
    }
    try:
        table = table_ref if table_ref is not None else _table()
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(inbox_id)",
        )
    except InboxError:
        return None
    except Exception:
        logger.warning("inbox record failed", extra={"inbox_id": item["inbox_id"]})
        return None
    return item["inbox_id"]


def complete(inbox_id, matched, *, error=None, table_ref=None):
    """Close out a recorded event with the workflows that matched.

    ``matched`` is the list of workflow ids the engine ran (None keeps any
    existing value — the failure path may not know). ``error`` marks the row
    failed with a short message; the SQS retry path rewrites it on success.
    No-op without an inbox id (recording was skipped) — best-effort like
    :func:`record`.
    """
    if not inbox_id:
        return None
    if error is not None:
        status, values = FAILED, {":status": FAILED, ":error": str(error)[:500]}
        sets = "#status = :status, #error = :error, processed_at = :at"
        names = {"#status": "status", "#error": "error"}
    elif matched:
        status, values = MATCHED, {":status": MATCHED, ":matched": [str(m) for m in matched]}
        sets = "#status = :status, matched = :matched, processed_at = :at"
        names = {"#status": "status"}
    else:
        status, values = UNMATCHED, {":status": UNMATCHED, ":matched": []}
        sets = "#status = :status, matched = :matched, processed_at = :at"
        names = {"#status": "status"}
    values[":at"] = _now_iso()
    try:
        table = table_ref if table_ref is not None else _table()
        table.update_item(
            Key={"inbox_id": str(inbox_id)},
            UpdateExpression="SET " + sets,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except InboxError:
        return None
    except Exception:
        logger.warning("inbox complete failed", extra={"inbox_id": inbox_id})
    return status


def note_replay_bounced(inbox_id, error, *, table_ref=None):
    """Record that a replay of this event failed before reaching the queue."""
    return complete(inbox_id, None, error=error, table_ref=table_ref)


def _view(item):
    keys = ("inbox_id", "connector", "event", "source", "occurred_at",
            "received_at", "processed_at", "status", "data", "matched", "error")
    return {key: _decode_numbers(item.get(key)) for key in keys}


def api_list(connector=None, limit=DEFAULT_LIMIT, *, table_ref=None):
    """Recent inbox events, newest first; ``connector`` filters when given."""
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    items = (table_ref if table_ref is not None else _table()).scan(
        Limit=max(limit * 6, 150)).get("Items", [])
    events = [_view(item) for item in items]
    if connector:
        events = [event for event in events if event.get("connector") == connector]
    events.sort(key=lambda event: event.get("received_at") or "", reverse=True)
    return 200, {"events": events[:limit], "total": len(events)}


def api_get(inbox_id, *, table_ref=None):
    inbox_id = str(inbox_id or "").strip()
    if not inbox_id:
        return 400, {"error": "inbox_id is required"}
    item = (table_ref if table_ref is not None else _table()).get_item(
        Key={"inbox_id": inbox_id}).get("Item")
    if not item:
        return 404, {"error": "Inbox event not found"}
    return 200, {"event": _view(item)}


def replay_event(item):
    """Rebuild the stored envelope for replay, or ``(None, error)``.

    A fresh event id (fresh run in history) with ``correlation_id`` pointing
    at the original event, mirroring the run-history replay.
    """
    data = item.get("data")
    if isinstance(data, dict) and data.get("truncated"):
        return None, "The event data was too large to store; it cannot be replayed"
    event_id = f"inbox-replay-{uuid.uuid4()}"
    return {
        "schema_version": "1.0",
        "id": event_id,
        "correlation_id": item.get("inbox_id"),
        "connector": item.get("connector"),
        "event": item.get("event"),
        "source": item.get("source"),
        "occurred_at": _now_iso(),
        "data": data if isinstance(data, dict) else {},
    }, None


def api_replay(inbox_id, *, queue=None, table_ref=None):
    """Re-send a recorded event through the engine (asynchronous, 202)."""
    inbox_id = str(inbox_id or "").strip()
    if not inbox_id:
        return 400, {"error": "inbox_id is required"}
    try:
        table = table_ref if table_ref is not None else _table()
    except InboxError as exc:
        return 503, {"error": str(exc)}
    item = table.get_item(Key={"inbox_id": inbox_id}).get("Item")
    if not item:
        return 404, {"error": "Inbox event not found"}
    event, error = replay_event(_decode_numbers(item))
    if error:
        return 409, {"error": error}
    try:
        (queue or _sqs()).send_message(
            QueueUrl=os.environ["EVENT_QUEUE_URL"],
            MessageBody=json.dumps(event),
        )
    except InboxError as exc:
        return 503, {"error": str(exc)}
    return 202, {
        "accepted": True,
        "replayed_from": inbox_id,
        "event_id": event["id"],
        "run_id": f"{item.get('connector')}:{event['id']}",
    }


def _sqs():
    import boto3

    return boto3.client("sqs")


def dedupe_skip_statuses():
    """Step statuses a redelivery may treat as done (engine.worker mirror)."""
    return _DONE_STATUSES
