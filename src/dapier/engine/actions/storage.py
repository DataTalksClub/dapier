"""storage_get / storage_set / storage_delete / storage_find: per-workflow
key-value state in DynamoDB.

Zapier's Storage: cross-run memory for a workflow. Every item lives under
the workflow's own partition — partition key ``scope`` is the workflow id,
sort key ``key`` is the stored key — so one table serves every workflow and
no workflow can read another's values. An item carries ``value`` (a string),
``updated_at`` (ISO) and, when the set asked for a TTL, ``expires`` (epoch
seconds — the table's TTL attribute; DynamoDB deletes expired items in the
background).

The table name comes from the ``STORAGE_TABLE`` environment variable; the
runners raise a clear ValueError when it is unset, and when a run somehow
arrives without a workflow id (the scope).
"""
import os
import time
from datetime import datetime, timezone

from .templating import render

FIND_DEFAULT_LIMIT = 20
FIND_MAX_LIMIT = 50


def _table():
    """The workflow-state table named by ``STORAGE_TABLE``."""
    name = str(os.environ.get("STORAGE_TABLE") or "").strip()
    if not name:
        raise ValueError(
            "the storage actions need the STORAGE_TABLE environment variable "
            "(the DynamoDB table that holds per-workflow key-value state)")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def _scope(workflow_id):
    """The workflow's id as the item's partition key."""
    scope = str(workflow_id or "").strip()
    if not scope:
        raise ValueError("storage actions need a workflow id to scope the values to")
    return scope


def _key(action, runner):
    key = str(action.get("key") or "").strip()
    if not key:
        raise ValueError(f"{runner} requires a key")
    return key


def _kv_key(key, *, allow_empty=False):
    key = str(key or "").strip()
    if not key and not allow_empty:
        raise ValueError("storage actions need a non-empty key")
    return key


def _ttl(ttl_seconds):
    """``ttl_seconds`` as an int, or None when unset (see Action: number field)."""
    if ttl_seconds is None or (isinstance(ttl_seconds, str) and not ttl_seconds.strip()):
        return None
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise ValueError("storage_set ttl_seconds must be a whole number of seconds")
    if ttl < 1:
        raise ValueError("storage_set ttl_seconds must be at least 1")
    return ttl


# The plain KV layer under the runners: the /api/*/storage endpoints drive
# the same table and rules through these, so console, CLI and runs cannot
# drift apart (UI/CLI parity — AGENTS.md).

def kv_get(scope, key):
    """The stored item for ``key`` under ``scope``, or None."""
    return _table().get_item(
        Key={"scope": _scope(scope), "key": _kv_key(key)}).get("Item") or None


def kv_set(scope, key, value, ttl_seconds=None):
    """Store ``value`` (already rendered) under ``key``; returns the item."""
    item = {
        "scope": _scope(scope),
        "key": _kv_key(key),
        "value": str(value),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    ttl = _ttl(ttl_seconds)
    if ttl is not None:
        item["expires"] = int(time.time()) + ttl
    _table().put_item(Item=item)
    return item


def kv_delete(scope, key):
    """Remove ``key``; True when an item was actually there."""
    response = _table().delete_item(
        Key={"scope": _scope(scope), "key": _kv_key(key)}, ReturnValues="ALL_OLD")
    return bool(response.get("Attributes"))


def kv_find(scope, prefix, limit=None):
    """Items under ``scope`` whose key starts with ``prefix``, ascending by key."""
    count = FIND_MAX_LIMIT if limit is None else max(1, min(int(limit), FIND_MAX_LIMIT))
    from boto3.dynamodb.conditions import Key

    response = _table().query(
        KeyConditionExpression=Key("scope").eq(_scope(scope))
        & Key("key").begins_with(_kv_key(prefix, allow_empty=True)),
        Limit=count,
    )
    return response.get("Items") or []


def _rendered_value(action, event, steps):
    """``value`` as a stored string, template-rendered like the sheets actions."""
    raw = action.get("value")
    if raw is None:
        raise ValueError("storage_set requires a value")
    return render(raw, event, steps) if isinstance(raw, str) else str(raw)


def run_storage_get(action, event, workflow_id, steps=None):
    """Read this workflow's value for ``key``.

    A missing key is a result, not an error:
    ``{"key": ..., "value": "", "found": False}``.
    """
    key = _key(action, "storage_get")
    item = kv_get(workflow_id, key) or {}
    return {"key": key, "value": str(item.get("value") or ""), "found": bool(item)}


def run_storage_set(action, event, workflow_id, steps=None):
    """Store ``value`` under ``key`` for this workflow (overwrite is a plain put).

    ``value`` is template-rendered from the event and earlier steps; optional
    ``ttl_seconds`` stamps ``expires`` = now + ttl so the table's TTL cleans
    the item up. Output: ``{"key", "stored": True}`` plus ``expires`` when set.
    """
    key = _key(action, "storage_set")
    ttl = _ttl(action.get("ttl_seconds"))
    item = kv_set(workflow_id, key, _rendered_value(action, event, steps), ttl)
    output = {"key": key, "stored": True}
    if ttl is not None:
        output["expires"] = item["expires"]
    return output


def run_storage_delete(action, event, workflow_id, steps=None):
    """Remove ``key`` from this workflow's values.

    ``deleted`` reports whether an item was actually there — deleting a
    missing key is fine.
    """
    key = _key(action, "storage_delete")
    return {"key": key, "deleted": kv_delete(workflow_id, key)}


def _find_limit(action):
    raw = str(action.get("limit") or "").strip()
    if not raw:
        return FIND_DEFAULT_LIMIT
    try:
        limit = int(raw)
    except ValueError:
        raise ValueError("storage_find limit must be a whole number")
    if limit < 1:
        raise ValueError("storage_find limit must be at least 1")
    return min(limit, FIND_MAX_LIMIT)


def run_storage_find(action, event, workflow_id, steps=None):
    """List this workflow's keys under ``prefix``, ascending by key.

    One Query (scope equals the workflow, key begins with the prefix), capped
    by ``limit`` (default 20, at most 50). Output: ``{"items": [{"key",
    "value"}...], "count"}``.
    """
    prefix = str(action.get("prefix") or "").strip()
    if not prefix:
        raise ValueError("storage_find requires a prefix")
    items = kv_find(workflow_id, prefix, _find_limit(action))
    return {
        "items": [
            {"key": str(item.get("key") or ""), "value": str(item.get("value") or "")}
            for item in items
        ],
        "count": len(items),
    }
