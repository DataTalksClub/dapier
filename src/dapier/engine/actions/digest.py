"""digest_add / digest_flush: collect items across runs, release them as one.

Zapier's Digest: events trickle in from several runs and a scheduled run
releases them together as one list. Items live in the same per-workflow
table the storage actions use (``STORAGE_TABLE``), under keys
``digest/<digest-key>/<arrival-seq>`` — the sequence is a UTC timestamp, so
one Query returns the items in arrival order and a flush can drain the
digest by deleting exactly what it read.

A digest holds at most ``max_items`` pending items (default
``DIGEST_DEFAULT_MAX_ITEMS`` = 500); a further add drops the oldest items
to make room, so a full digest never blocks a run — anything not flushed
within ``max_items`` arrivals is lost, which is the tradeoff against
failing the run the way a hard cap would. Like ``storage_set``, an append
is a read-count-then-put (no conditional write), so two concurrent appends
can interleave; order within one digest is best-effort across concurrent
runs and exact within one.

``dedupe: true`` turns a repeat item into a no-op instead of a second
entry — the common "collect each sender once" shape. Omitting ``item``
stores the event's data payload as JSON, so ``{item.field}`` templates
work on it after the flush. ``flush`` with ``reset: false`` peeks at the
digest without clearing it.
"""
import json
from datetime import datetime, timezone
from itertools import count

from .storage import _scope, _table, _ttl, kv_delete, kv_set
from .templating import render

DIGEST_DEFAULT_MAX_ITEMS = 500

# Breaks same-microsecond ties so arrival order is total within a process;
# across processes the timestamp prefix orders.
_SEQ = count()


def _digest_key(action, runner):
    key = str(action.get("key") or "").strip()
    if not key:
        raise ValueError(f"{runner} requires a key")
    return key


def _prefix(key):
    return f"digest/{key}/"


def _flag(action, key):
    """Designer boolean fields arrive as "true"/"false" strings."""
    value = action.get(key)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _clears(action):
    """Whether a flush clears what it read — ``reset`` defaults to true."""
    raw = action.get("reset")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return True
    return _flag(action, "reset")


def _deep_render(value, event, steps):
    """Templates render inside JSON items too, at any depth."""
    if isinstance(value, str):
        return render(value, event, steps)
    if isinstance(value, dict):
        return {k: _deep_render(v, event, steps) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep_render(v, event, steps) for v in value]
    return value


def _rendered_item(action, event, steps):
    """``item`` as the stored string; dict/list items keep their JSON shape.

    With no ``item`` configured the event's data payload is stored as JSON,
    so the whole event rides the digest and ``{item.field}`` still works
    after the flush (values are stored as strings, like storage_set).
    """
    raw = action.get("item")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        data = event.get("data") if isinstance(event, dict) else None
        return json.dumps(data if data is not None else event)
    if isinstance(raw, str):
        return render(raw, event, steps)
    if isinstance(raw, (dict, list)):
        return json.dumps(_deep_render(raw, event, steps))
    return json.dumps(raw)


def _max_items(action):
    """``max_items`` as an int, defaulting to ``DIGEST_DEFAULT_MAX_ITEMS``."""
    raw = action.get("max_items")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DIGEST_DEFAULT_MAX_ITEMS
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        raise ValueError("digest_add max_items must be a whole number")
    if cap < 1:
        raise ValueError("digest_add max_items must be at least 1")
    return cap


def _pending_count(workflow_id, prefix):
    """How many items the digest holds (a COUNT query — exact, no page cap)."""
    from boto3.dynamodb.conditions import Key

    response = _table().query(
        KeyConditionExpression=Key("scope").eq(_scope(workflow_id))
        & Key("key").begins_with(prefix),
        Select="COUNT",
    )
    return int(response.get("Count") or 0)


def _pending(workflow_id, prefix, limit):
    """The digest's items, oldest first, up to ``limit`` of them.

    One Query page — items are short strings or JSON lines, so the default
    500-item cap stays well inside DynamoDB's 1MB page.
    """
    from boto3.dynamodb.conditions import Key

    response = _table().query(
        KeyConditionExpression=Key("scope").eq(_scope(workflow_id))
        & Key("key").begins_with(prefix),
        Limit=max(1, int(limit)),
    )
    return response.get("Items") or []


def _maybe_json(value):
    """Structured items come back structured; plain strings stay strings."""
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return value
    return parsed if isinstance(parsed, (dict, list)) else value


def run_digest_add(action, event, workflow_id, steps=None):
    """Append ``item`` to this workflow's digest under ``key``.

    ``dedupe`` skips an item whose stored value is already pending. Beyond
    ``max_items`` (default 500) the oldest items are dropped to make room —
    see the module docstring for the tradeoff. The output reports the
    digest's new size either way: ``{"key", "added", "count"}``.
    """
    key = _digest_key(action, "digest_add")
    item = _rendered_item(action, event, steps)
    cap = _max_items(action)
    prefix = _prefix(key)
    total = _pending_count(workflow_id, prefix)
    if _flag(action, "dedupe") and any(
            p.get("value") == item for p in _pending(workflow_id, prefix, cap)):
        return {"key": key, "added": False, "count": total}
    over = total + 1 - cap
    for old in _pending(workflow_id, prefix, over) if over > 0 else ():
        kv_delete(workflow_id, str(old.get("key") or ""))
    arrival = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
               + f"{next(_SEQ):06d}")
    kv_set(workflow_id, f"{prefix}{arrival}", item, _ttl(action.get("ttl_seconds")))
    return {"key": key, "added": True, "count": total + 1 - max(0, over)}


def run_digest_flush(action, event, workflow_id, steps=None):
    """Release this workflow's digest under ``key`` and empty it.

    Items come back in arrival order as ``{"key", "items", "count",
    "empty"}`` — pair ``empty`` with a filter step to stop a scheduled run
    that has nothing to release. ``reset: false`` peeks: same output, the
    digest stays intact for the next flush. Deleting what was read makes
    the default flush idempotent: a replayed run releases nothing.
    """
    key = _digest_key(action, "digest_flush")
    prefix = _prefix(key)
    total = _pending_count(workflow_id, prefix)
    pending = _pending(workflow_id, prefix, total) if total else []
    items = [_maybe_json(str(p.get("value") or "")) for p in pending]
    if _clears(action):
        for p in pending:
            kv_delete(workflow_id, str(p.get("key") or ""))
    return {"key": key, "items": items, "count": len(items), "empty": not items}
