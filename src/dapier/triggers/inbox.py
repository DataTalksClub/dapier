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

import base64
import json
import logging
import os
import uuid
from datetime import datetime, timezone

from ..auth import visibility

logger = logging.getLogger(__name__)

TABLE_ENV = "TRIGGER_INBOX_TABLE"
RETENTION_DAYS = 30
DEFAULT_LIMIT = 25
MAX_LIMIT = 200

# Stored event data is the trigger envelope, capped like the run-history
# trigger input (engine.worker.TRIGGER_INPUT_LIMIT): high enough that an
# ordinary webhook or email event replays, low enough that the item stays
# far below the DynamoDB 400 KB limit. Oversize-still data keeps the
# explicit replay refusal.
DATA_LIMIT = 65_000

# A row the worker has not closed out yet; the run is in flight or the
# worker died mid-run (the retry heals the row via complete()).
RECEIVED = "received"
MATCHED = "matched"
UNMATCHED = "unmatched"
FAILED = "failed"
IGNORED = "ignored"

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


def _trim_event_data(data, limit=DATA_LIMIT):
    """Cap stored event data like :func:`_trim`, but keep the replay
    pointers: an oversized email event stays replayable through the raw
    s3 copy its pointer names."""
    trimmed = _trim(data, limit=limit)
    if not (isinstance(trimmed, dict) and trimmed.get("truncated")):
        return trimmed
    if isinstance(data, dict):
        for key in ("s3", "message_id"):
            if data.get(key) is not None:
                trimmed[key] = data[key]
    return trimmed


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
        "data": _trim_event_data(event.get("data") or {}),
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


def ignore(inbox_id, *, table_ref=None):
    """Close an event that the sender list refused. No workflow runs."""
    if not inbox_id:
        return None
    try:
        table = table_ref if table_ref is not None else _table()
        table.update_item(
            Key={"inbox_id": inbox_id},
            UpdateExpression="SET #status = :status, matched = :matched, processed_at = :at",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": IGNORED,
                ":matched": [],
                ":at": _now_iso(),
            },
        )
    except InboxError:
        return None
    except Exception:
        logger.warning("inbox ignore failed", extra={"inbox_id": inbox_id})
        return None
    return inbox_id


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


def _sort_key(event):
    """Newest-first ordering with an inbox-id tiebreak, so paging is stable."""
    return (event.get("received_at") or "", event.get("inbox_id") or "")


def _encode_token(event):
    """Opaque stateless page token: the last row's sort key."""
    raw = json.dumps({"r": event.get("received_at") or "",
                      "i": event.get("inbox_id") or ""},
                     sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_token(token):
    """The ``(received_at, inbox_id)`` sort key behind a page token, or None."""
    text = str(token or "")
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        key = (str(data.get("r") or ""), str(data.get("i") or ""))
    except (ValueError, TypeError):
        return None
    return key if key != ("", "") else None


def api_list(connector=None, limit=DEFAULT_LIMIT, *, next_token=None, table_ref=None,
             visible=None):
    """Recent inbox events, newest first; ``connector`` filters when given.

    ``paging.next`` carries the last returned row's sort key; the follow-up
    call passes it back as ``next_token`` and the window starts strictly
    after that row, so the connector filter and paging compose. The scanned
    window stays bounded like every list call — events older than the window
    age out of the list but stay reachable through api_get.

    ``visible`` (an auth.visibility.Visibility, None = unrestricted)
    read-filters the list, G17 Phase 2: a non-operator keeps the events that
    matched at least one workflow it owns. An event nothing matched is
    nobody's row — visible to everyone, like every no-owner item.
    """
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    token_key = _decode_token(next_token) if next_token else None
    if next_token and token_key is None:
        return 400, {"error": "Invalid page token"}
    owners = visibility.owners_for(visible)
    items = (table_ref if table_ref is not None else _table()).scan(
        Limit=max(limit * 6, 150)).get("Items", [])
    events = [_view(item) for item in items]
    if connector:
        events = [event for event in events if event.get("connector") == connector]
    if visible is not None:
        events = [event for event in events if _visible_event(event, visible, owners)]
    events.sort(key=_sort_key, reverse=True)
    if token_key is not None:
        events = [event for event in events if _sort_key(event) < token_key]
    page = events[:limit]
    more = len(events) > limit
    return 200, {
        "events": page,
        "total": len(events),
        "paging": {
            "next": _encode_token(page[-1]) if more and page else None,
            "limit": limit,
            "filtered": bool(connector or next_token),
        },
    }


def _visible_event(event, visible, owners):
    """The G17 rule for one inbox row: visible when it matched a workflow
    the caller may see, and when it matched nothing at all (no owner — the
    defensive rule that never hides an unclaimed event)."""
    matched = event.get("matched") or []
    if not matched:
        return True
    return any(visible.workflow_visible(m, owners) for m in matched)


def api_get(inbox_id, *, table_ref=None, visible=None):
    inbox_id = str(inbox_id or "").strip()
    if not inbox_id:
        return 400, {"error": "inbox_id is required"}
    item = (table_ref if table_ref is not None else _table()).get_item(
        Key={"inbox_id": inbox_id}).get("Item")
    if not item:
        return 404, {"error": "Inbox event not found"}
    if visible is not None and not _visible_event(
            _view(item), visible, visibility.owners_for(visible)):
        # Same answer as a missing row: the list dropped it, the single
        # read must not reveal it either.
        return 404, {"error": "Inbox event not found"}
    return 200, {"event": _view(item)}


def stored(inbox_id, *, table_ref=None):
    """The stored inbox row for one event id, decoded, or None.

    Read for the replay paths: a run whose step input was clipped falls
    back to the inbox row's s3 pointer to rehydrate its trigger data.
    """
    inbox_id = str(inbox_id or "").strip()
    if not inbox_id:
        return None
    try:
        table = table_ref if table_ref is not None else _table()
    except InboxError:
        return None
    item = table.get_item(Key={"inbox_id": inbox_id}).get("Item")
    return _decode_numbers(item) if item else None


def rehydrate(data):
    """Full event data for a truncated stored row, rebuilt from the raw
    email copy its ``s3`` pointer names. Returns ``(data, None)``, or
    ``(None, error)`` when there is no pointer or the copy is unreadable —
    callers keep their explicit replay refusal in that case."""
    pointer = data.get("s3") if isinstance(data, dict) else None
    if not pointer:
        return None, "The event data was too large to store; it cannot be replayed"
    from .intake import email_ingress

    full, error = email_ingress.replay_data(pointer)
    if error:
        return None, f"The stored raw email {error}; it cannot be replayed"
    return full, None


def replay_event(item):
    """Rebuild the stored envelope for replay, or ``(None, error)``.

    A fresh event id (fresh run in history) with ``correlation_id`` pointing
    at the original event, mirroring the run-history replay. Truncated data
    rehydrates from the raw s3 copy its pointer names.
    """
    data = item.get("data")
    if isinstance(data, dict) and data.get("truncated"):
        data, error = rehydrate(data)
        if error:
            return None, error
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
