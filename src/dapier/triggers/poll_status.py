"""Poll health: what each poll trigger's last checks found.

A poll's cursor says where it is; nothing said whether it is still working.
Every fire records one row in the cursors table (``CURSORS_TABLE`` — where
the poll cursors and seen-sets already live, no new infrastructure) under
its own ``pollstat#`` prefix:

    cursor_id      = "pollstat#<poll_id>"
    last_checked_at  when the poll last ran (scheduled or Poll now)
    last_reason      "schedule" | "manual"
    last_ok_at       the last check that finished without an error
    last_found       new items the last check started workflows for
    last_new_at      the last check that found at least one new item
    last_new_count   how many it found then
    failures         consecutive failed checks (0 after a clean one)
    last_error / last_error_at   the most recent failure

Recording is best-effort: a status write must never fail a fire, and it
no-ops when the cursors table is not configured. :func:`health` turns a row
into the one word the console and the CLI show.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

TABLE_ENV = "CURSORS_TABLE"
PREFIX = "pollstat#"
ERROR_LIMIT = 500

OK = "ok"
FAILING = "failing"
PAUSED = "paused"
WAITING = "waiting"
LATE = "late"

# A rate-scheduled poll that has not checked in this many intervals is late:
# its EventBridge rule is not firing (or the worker never got there).
LATE_INTERVALS = 3
_RATE = re.compile(r"^rate\((\d+)\s+(minute|minutes|hour|hours|day|days)\)$")
_UNIT_SECONDS = {"minute": 60, "hour": 3600, "day": 86400}


def scope_id(poll_id):
    return f"{PREFIX}{poll_id}"


def _table(table_ref=None):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(TABLE_ENV)
    if not name:
        return None
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _now_iso(now=None):
    return (now or datetime.now(timezone.utc)).isoformat()


def record(poll_id, *, found=0, error=None, reason="schedule", table_ref=None, now=None):
    """Record one check. ``error`` marks it failed; ``found`` counts the new
    items the check started workflows for (they still count on a check that
    failed part-way). Never raises."""
    try:
        table = _table(table_ref)
        if table is None:
            return None
        at = _now_iso(now)
        sets = ["last_checked_at = :at", "last_reason = :reason",
                "last_found = :found", "updated_at = :at"]
        values = {":at": at, ":reason": str(reason or "schedule"), ":found": int(found or 0)}
        if found:
            sets += ["last_new_at = :at", "last_new_count = :found"]
        if error is not None:
            sets += ["last_error = :error", "last_error_at = :at"]
            values[":error"] = (str(error) or error.__class__.__name__)[:ERROR_LIMIT] \
                if isinstance(error, BaseException) else str(error)[:ERROR_LIMIT]
            values[":one"] = 1
            values[":zero"] = 0
            sets.append("failures = if_not_exists(failures, :zero) + :one")
        else:
            sets += ["last_ok_at = :at", "failures = :zero"]
            values[":zero"] = 0
        table.update_item(
            Key={"cursor_id": scope_id(poll_id)},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeValues=values,
        )
        return True
    except Exception:  # noqa: BLE001 — status is bookkeeping, never a fire failure
        logger.warning("poll status record failed", extra={"poll_id": poll_id})
        return None


def _decode(value):
    from decimal import Decimal

    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def get(poll_id, *, table_ref=None):
    """The poll's status row as a plain dict ({} when never checked or the
    store is unavailable)."""
    try:
        table = _table(table_ref)
        if table is None:
            return {}
        item = table.get_item(Key={"cursor_id": scope_id(poll_id)}).get("Item") or {}
    except Exception:  # noqa: BLE001 — a status read must not break the list
        logger.warning("poll status read failed", extra={"poll_id": poll_id})
        return {}
    keys = ("last_checked_at", "last_reason", "last_ok_at", "last_found",
            "last_new_at", "last_new_count", "failures", "last_error", "last_error_at")
    return {key: _decode(item[key]) for key in keys if key in item}


def drop(poll_id, *, table_ref=None):
    """Forget a deleted poll's status, best-effort."""
    try:
        table = _table(table_ref)
        if table is not None:
            table.delete_item(Key={"cursor_id": scope_id(poll_id)})
    except Exception:  # noqa: BLE001
        logger.warning("poll status drop failed", extra={"poll_id": poll_id})


def interval_seconds(expression):
    """A ``rate(...)`` schedule's interval in seconds, or None for cron."""
    match = _RATE.match(str(expression or "").strip())
    if not match:
        return None
    unit = match.group(2).rstrip("s")
    return int(match.group(1)) * _UNIT_SECONDS[unit]


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def health(item, status, *, now=None):
    """One word for the poll's state: ``paused`` (switched off), ``waiting``
    (enabled but never checked yet), ``failing`` (the last check errored),
    ``late`` (a rate schedule missed several checks in a row), else ``ok``."""
    if not item.get("enabled", True):
        return PAUSED
    if not status.get("last_checked_at"):
        return WAITING
    if int(status.get("failures") or 0) > 0:
        return FAILING
    interval = interval_seconds(item.get("expression"))
    checked = _parse(status.get("last_checked_at"))
    now = now or datetime.now(timezone.utc)
    if interval and checked and now - checked > timedelta(seconds=interval * LATE_INTERVALS):
        return LATE
    return OK
