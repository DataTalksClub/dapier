"""Seen-id store: a TTL-bounded, per-scope set of already-published event ids.

Two dedupe gaps share this store. Poll triggers in ``next_cursor`` mode
re-list items across fires (the continuation cursor advances, but providers
recycle pages), so each item needs an "already published" marker beside the
cursor; and stored hooks with a ``dedupe_path`` must not double-run when a
provider retries the same delivery. Both record event ids here keyed by a
per-trigger scope and both expire: entries carry an epoch ``expires_at``
(the trigger inbox's TTL pattern), pruned on every write; the set is capped
per scope; abandoned scopes age out via the item's own TTL attribute.

One item per scope in the cursors table (``CURSORS_TABLE`` — the table poll
cursors already live in, no new infrastructure), reusing its ``cursor_id``
hash key with a ``seen#`` prefix so the rows never collide with cursors:

    cursor_id  = "seen#<scope>"      e.g. seen#poll#orders, seen#hook#orders
    seen       = {"<event id>": <expires epoch>, ...}
    expires_at / updated_at          bookkeeping (expires_at is the table's TTL)

:func:`claim` is the atomic primitive for the publish path (a conditional
put, the trigger-inbox record's pattern): True = first sighting, now
recorded; False = an id still inside its TTL, so the caller skips the
duplicate.
"""

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

TABLE_ENV = "CURSORS_TABLE"
# How long a seen id blocks its duplicate — the dedupe window both the poll
# fires and the hook claims share (a trigger may tighten it per-trigger via
# ``dedupe_ttl_days``).
TTL_DAYS = 7
MAX_ENTRIES = 1000
PREFIX = "seen#"


class StoreError(Exception):
    """The seen store is not configured (no cursors table)."""


def _now():
    return int(datetime.now(timezone.utc).timestamp())


def scope_id(scope):
    return f"{PREFIX}{scope}"


def get_table(table_ref=None):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(TABLE_ENV)
    if not name:
        raise StoreError("the seen store needs the cursors table")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def prune(entries, *, now=None, max_entries=MAX_ENTRIES):
    """Drop expired entries, then keep only the ``max_entries`` newest."""
    now = _now() if now is None else now
    live = {key: expires for key, expires in (entries or {}).items()
            if expires > now}
    if len(live) > max_entries:
        newest = sorted(live.items(), key=lambda entry: entry[1], reverse=True)
        live = dict(newest[:max_entries])
    return live


def _store(table, scope, entries):
    """Write one scope's pruned seen set (a fresh map, never a shared one)."""
    table.put_item(Item={
        # The cursors table's hash key is cursor_id; the seen# prefix keeps
        # these rows disjoint from the poll# cursor rows.
        "cursor_id": scope_id(scope),
        "seen": dict(entries),
        "expires_at": max(entries.values()),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def load(scope, *, table_ref=None, now=None, max_entries=MAX_ENTRIES):
    """The scope's live seen set, pruned of expired entries."""
    item = get_table(table_ref).get_item(Key={"cursor_id": scope_id(scope)}).get("Item") or {}
    return prune(item.get("seen") or {}, now=now, max_entries=max_entries)


def remember(scope, key, entries=None, *, table_ref=None, now=None,
             ttl_days=TTL_DAYS, max_entries=MAX_ENTRIES):
    """Record one id and persist the pruned set; returns the updated set.

    ``entries`` — the caller's in-memory copy from a previous
    :func:`load`/:func:`remember` — is merged over a fresh read, so a
    concurrent writer's additions survive; callers keep the return as their
    running copy. Callers that must not mark an id seen until its work
    succeeded (the poll fire, where a failed item retries) call this after
    the fact; :func:`claim` is the check-and-mark-in-one for publish paths.
    """
    now = _now() if now is None else now
    merged = dict(entries or {})
    merged.update(load(scope, table_ref=table_ref, now=now, max_entries=max_entries))
    merged[key] = now + ttl_days * 86400
    merged = prune(merged, now=now, max_entries=max_entries)
    _store(get_table(table_ref), scope, merged)
    return merged


def claim(scope, key, *, table_ref=None, now=None, ttl_days=TTL_DAYS,
          max_entries=MAX_ENTRIES):
    """Atomically claim an id: True = new (recorded), False = seen and live.

    A conditional update decides without a prior read and sets one map key in
    place — a plain put would pass its condition whenever the key was simply
    new and replace the item, dropping every sibling entry in the scope. When
    the condition fails the entry may still be free — expired since it was
    written, or pruned by a concurrent writer — so the loser re-reads and, if
    the id is genuinely available again, rewrites the pruned set and claims it.
    """
    from botocore.exceptions import ClientError

    now = _now() if now is None else now
    expires = now + ttl_days * 86400
    table = get_table(table_ref)
    try:
        table.update_item(
            Key={"cursor_id": scope_id(scope)},
            UpdateExpression="SET #m.#k = :expires, #e = :expires, #u = :at",
            ConditionExpression="attribute_not_exists(#m.#k)",
            ExpressionAttributeNames={"#m": "seen", "#k": key,
                                      "#e": "expires_at", "#u": "updated_at"},
            ExpressionAttributeValues={":expires": expires,
                                       ":at": datetime.now(timezone.utc).isoformat()},
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    entries = table.get_item(Key={"cursor_id": scope_id(scope)}).get("Item",
                                                                     {}).get("seen") or {}
    if entries.get(key, 0) > now:
        return False
    merged = prune(entries, now=now, max_entries=max_entries)
    merged[key] = expires
    _store(table, scope, merged)
    return True


def forget(scope, key, *, table_ref=None, now=None, max_entries=MAX_ENTRIES):
    """Release a claim, best-effort: the publish that followed it failed, so
    a provider retry must be able to deliver. Never raises."""
    try:
        table = get_table(table_ref)
        item = table.get_item(Key={"cursor_id": scope_id(scope)}).get("Item") or {}
        entries = prune(item.get("seen") or {}, now=now, max_entries=max_entries)
        if key not in entries:
            return
        del entries[key]
        if entries:
            _store(table, scope, entries)
        else:
            table.delete_item(Key={"cursor_id": scope_id(scope)})
    except Exception:
        logger.warning("seen forget failed", extra={"scope": scope, "key": key})


def drop(scope, *, table_ref=None):
    """Forget a whole scope, best-effort: the trigger behind it was deleted,
    so a recreate starts clean instead of inheriting stale seen ids. Never
    raises (teardown must not fail because the store is unavailable)."""
    try:
        get_table(table_ref).delete_item(Key={"cursor_id": scope_id(scope)})
    except Exception:
        logger.warning("seen drop failed", extra={"scope": scope})
