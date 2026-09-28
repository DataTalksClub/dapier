"""Consecutive-failure counter: the per-workflow auto-pause trip wire.

Zapier turns a zap off after repeated errors; dapier counts consecutive
failed runs per workflow and the worker pauses the workflow when the count
reaches the definition's ``auto_pause_after`` threshold (engine.worker owns
the threshold and the pause itself). One item per workflow in the cursors
table (``CURSORS_TABLE`` — the table poll cursors and the seen sets already
live in, no new infrastructure), under its own ``fails#`` prefix so the rows
never collide:

    cursor_id  = "fails#<workflow_id>"
    fails      = <consecutive failed runs>
    last_error = <summary of the last failure>
    last_event = <event id already counted — a redelivered record of the
                  same failed event must not count twice>
    last_failed_at / updated_at / expires_at   bookkeeping (expires_at is
                                               the table's TTL)

A completed run resets the streak (the item is deleted); the flag the trip
sets lives on the stored workflow definition, not here, so an expired
counter can never silently resume a paused workflow.
"""

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TABLE_ENV = "CURSORS_TABLE"
PREFIX = "fails#"
# How long an unreset counter lingers — runtime state, not history. A paused
# workflow stays paused regardless (the flag is on the definition), so this
# only bounds a forgotten row.
TTL_DAYS = 30
ERROR_LIMIT = 500


class StoreError(Exception):
    """The failure counter is not configured (no cursors table)."""


def _now():
    return int(datetime.now(timezone.utc).timestamp())


def scope_id(workflow_id):
    return f"{PREFIX}{workflow_id}"


def get_table(table_ref=None):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(TABLE_ENV)
    if not name:
        raise StoreError("the failure counter needs the cursors table")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def record_failure(workflow_id, error="", *, event_id=None, threshold=None,
                   table_ref=None, now=None):
    """Count one failed run; returns ``(count, tripped)``.

    The increment is one atomic conditional update (``ADD fails :one``, the
    new item back via ``ReturnValues``), so concurrent failures of one
    workflow cannot lose a count. The condition is the redelivery guard: a
    failed record is retried until it leaves the queue, so the event that
    already counted (``last_event``) reports the current count without
    counting again — one event can trip the pause at most once.
    ``tripped`` is True only when this call's own increment reached
    ``threshold`` (the worker resolves it from the workflow's YAML; None
    counts but never trips).
    """
    from botocore.exceptions import ClientError

    now = _now() if now is None else int(now)
    iso = datetime.now(timezone.utc).isoformat()
    table = get_table(table_ref)
    try:
        result = table.update_item(
            Key={"cursor_id": scope_id(workflow_id)},
            UpdateExpression=("SET last_error = :error, last_event = :event, "
                              "last_failed_at = :at, updated_at = :at, "
                              "expires_at = :expires ADD #fails :one"),
            # A fresh event counts; the event this counter already recorded
            # (a redelivery of the failed record) does not.
            ConditionExpression="attribute_not_exists(last_event) OR last_event <> :event",
            ExpressionAttributeNames={"#fails": "fails"},
            ExpressionAttributeValues={
                ":error": str(error or "")[:ERROR_LIMIT],
                ":event": str(event_id or ""),
                ":at": iso,
                ":expires": now + TTL_DAYS * 86400,
                ":one": 1,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        item = table.get_item(Key={"cursor_id": scope_id(workflow_id)}).get("Item") or {}
        return int(item.get("fails") or 0), False
    count = int((result.get("Attributes") or {}).get("fails") or 0)
    return count, threshold is not None and count >= threshold


def reset(workflow_id, *, table_ref=None):
    """Zero the streak — a run succeeded, so counting starts over.

    Best-effort, and quiet when the cursors table is not wired: this runs
    on the success path, which must never learn about bookkeeping problems.
    """
    if table_ref is None and not os.environ.get(TABLE_ENV):
        return
    try:
        get_table(table_ref).delete_item(Key={"cursor_id": scope_id(workflow_id)})
    except Exception:
        logger.warning("failure-counter reset failed",
                       extra={"workflow_id": workflow_id})


def count(workflow_id, *, table_ref=None):
    """The workflow's current consecutive-failure count (0 when none)."""
    item = get_table(table_ref).get_item(Key={"cursor_id": scope_id(workflow_id)}).get("Item") or {}
    return int(item.get("fails") or 0)


def all_counts(table_ref=None):
    """Every workflow's current count as ``{workflow_id: fails}`` — one scan
    for the overview's per-workflow rows. Empty when the store is not wired."""
    if table_ref is None and not os.environ.get(TABLE_ENV):
        return {}
    items, start = [], None
    while True:
        kwargs = {} if start is None else {"ExclusiveStartKey": start}
        page = get_table(table_ref).scan(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            break
    prefix = len(PREFIX)
    return {str(item["cursor_id"])[prefix:]: int(item.get("fails") or 0)
            for item in items
            if str(item.get("cursor_id") or "").startswith(PREFIX)}
