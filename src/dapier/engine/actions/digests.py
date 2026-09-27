"""Digest state: per-workflow accumulated batches in DynamoDB.

Zapier's Digest: cross-run accumulation. A workflow appends an item on
every trigger fire and a later run (usually a schedule trigger) flushes
the whole batch at once — one email with ten lines instead of ten emails.
Items live under a partition — partition key ``scope`` is the workflow id,
sort key ``key`` is the digest's name — so one table serves every workflow
and no workflow can read or flush another's digest. A step that sets
``shared: true`` opts into the ``SHARED_SCOPE`` partition instead: the
canonical digest pattern accumulates in the event's workflow and flushes
from a schedule-triggered one, which are two workflow ids sharing one key.
An item carries ``items`` (the accumulated list, appended atomically via
``list_append``) and ``updated_at`` (ISO).

Flushing is a claim-and-clear in one operation: ``digests_claim`` deletes
the item and returns what it held (``ReturnValues="ALL_OLD"``). DynamoDB
serializes concurrent writes, so of two flushes racing on the same digest
exactly one reads the batch and the other sees nothing — the batch is
never sent twice, and an appender racing a flush lands in the next digest
(the next append recreates the item from empty).

The table name comes from the ``DIGESTS_TABLE`` environment variable; the
helpers raise a clear ValueError when it is unset (unit tests and local
runs without the stack degrade instead of crashing) and when a run
somehow arrives without a workflow id (the scope).
"""
import os
from datetime import datetime, timezone


def _table():
    """The digest-state table named by ``DIGESTS_TABLE``."""
    name = str(os.environ.get("DIGESTS_TABLE") or "").strip()
    if not name:
        raise ValueError(
            "the digest step needs the DIGESTS_TABLE environment variable "
            "(the DynamoDB table that holds accumulated digest items)")
    import boto3

    return boto3.resource("dynamodb").Table(name)


# Partition for ``shared: true`` digests: not a legal workflow id (ids are
# flow filenames), so it can never collide with a per-workflow partition.
SHARED_SCOPE = "*shared*"


def _scope(workflow_id):
    """The item's partition key: a workflow id, or SHARED_SCOPE."""
    scope = str(workflow_id or "").strip()
    if not scope:
        raise ValueError("the digest step needs a workflow id to scope digests to")
    return scope


def _key(key):
    key = str(key or "").strip()
    if not key:
        raise ValueError("the digest step requires a key")
    return key


# The plain state layer under the logic step (engine.logic._run_digest):
# get/append/claim, so the engine and any future caller share one set of
# table rules.

def digests_get(scope, key):
    """The accumulated items of one digest, oldest first ([] when empty)."""
    item = _table().get_item(
        Key={"scope": _scope(scope), "key": _key(key)}).get("Item") or {}
    items = item.get("items")
    return list(items) if isinstance(items, list) else []


def digests_append(scope, key, batch):
    """Append ``batch`` (a list of rendered items) to the digest, atomically.

    ``list_append(if_not_exists(...))`` creates the item on first append and
    never drops an item a concurrent appender wrote between read and write.
    Returns the new total count.
    """
    if not isinstance(batch, list) or not batch:
        raise ValueError("digests_append needs a non-empty batch of items")
    response = _table().update_item(
        Key={"scope": _scope(scope), "key": _key(key)},
        UpdateExpression="SET #items = list_append(if_not_exists(#items, :empty), :batch), "
                         "#updated = :now",
        ExpressionAttributeNames={"#items": "items", "#updated": "updated_at"},
        ExpressionAttributeValues={
            ":empty": [],
            ":batch": list(batch),
            ":now": datetime.now(timezone.utc).isoformat(),
        },
        ReturnValues="UPDATED_NEW",
    )
    appended = (response.get("Attributes") or {}).get("items")
    return len(appended) if isinstance(appended, list) else len(batch)


def digests_claim(scope, key):
    """Claim-and-clear: delete the digest item, return the items it held.

    One atomic operation — a concurrent flush of the same digest deletes
    the item first and this returns [] (the batch is never delivered
    twice), while an append that lands after the claim starts the next
    digest.
    """
    response = _table().delete_item(
        Key={"scope": _scope(scope), "key": _key(key)}, ReturnValues="ALL_OLD")
    items = (response.get("Attributes") or {}).get("items")
    return list(items) if isinstance(items, list) else []
