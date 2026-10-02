"""Poll triggers: run any API on a schedule with a stored cursor.

A poll trigger is an EventBridge rule (the schedule machinery) plus a fetch
spec: an endpoint, the path of the list to watch, and how to recognize new
items. Each fire fetches one page, and every new item becomes its own
``poll``/``item.new`` event — the engine matches workflows (including the
trigger's bound actions) per item, so one trigger fans out into one run per
new item without bespoke connector code.

Two cursor modes, stored in CURSORS_TABLE under ``poll#{name}``:

- ``watermark`` (default): items carry a comparable id (``id_path`` — an
  incrementing number or an ISO timestamp both work); the stored cursor is
  the last emitted id and only strictly-greater items fire.
- ``next_cursor``: the response carries an opaque continuation cursor
  (``cursor_path``); it is stored and passed back as the ``cursor_query``
  query parameter on the next fire, and every returned page counts as new
  — deduped per item (a TTL-bounded seen-set) so recycled pages do not
  re-run items.

The cursor advances only after an item's workflows ran, so a failed action
stops the fire and the item is retried on the next one.

A stored item's ``source`` key (see ``poll_sources``) swaps the HTTP+JSON
fetcher for a registered provider fetch — ``s3`` (boto3),
``google-sheets.rows``, ``google-drive.files`` — on the same schedule,
cursor and dedupe machinery; the published events then carry the source's
connector and event names (``s3``/``file.created``) instead of the generic
``poll``/``item.new``, which is what makes the provider palette chips real
triggers.
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from . import poll_sources, seen
from .email_triggers import NAME_PATTERN, TriggerError, resolve_actions
from .schedule_triggers import validate_expression

logger = logging.getLogger(__name__)

TABLE_ENV = "POLL_TRIGGERS_TABLE"
CURSOR_TABLE_ENV = "CURSORS_TABLE"
WORKER_ARN_ENV = "WORKER_FUNCTION_ARN"
# DynamoDB scan page size, not a cutoff: load_items walks pages to exhaustion.
SCAN_LIMIT = 200
RULE_PREFIX = "dapier-poll-"
TARGET_ID = "dapier-worker"
POLL_CONNECTOR = "poll"
POLL_EVENT = "item.new"
CURSOR_KEY_PREFIX = "poll#"

CURSOR_MODES = ("watermark", "next_cursor")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_ITEMS_CAP = 100
URL_PATTERN = re.compile(r"^https?://\S+$")

# The stored item's identity/lifecycle keys: a poll source's fetch-spec
# extras may not touch them. The fetch-spec keys (url, method, headers,
# body, list_path, id_path, connection_id, cursor_mode, cursor_path,
# cursor_query, max_items, dedupe_ttl_days) are deliberately NOT here —
# build_item pops those out of the extras to validate source-provided
# defaults; everything else a source returns merges into the item verbatim.
SOURCE_LOCKED_KEYS = frozenset({
    "poll_id", "source", "expression", "description", "actions",
    "enabled", "created_by", "created_at", "updated_at",
})


def rule_name(poll_id):
    return f"{RULE_PREFIX}{poll_id}"


def workflow_id_for(item):
    return f"poll-trigger-{item['poll_id']}"


def _validate_path(value, label):
    path = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*", path):
        raise TriggerError(f"{label} must be a dotted path like 'data.items'")
    return path


def build_item(body, operator, previous=None):
    """Build the stored item for a create or edit."""
    if not isinstance(body, dict):
        raise TriggerError("request body must be an object")
    name = str(body.get("name") or "").strip().lower()
    if not NAME_PATTERN.fullmatch(name):
        raise TriggerError("poll name must be 2-32 chars: lowercase letters, digits, hyphens")
    previous = previous or {}
    if previous and previous.get("poll_id") != name:
        raise TriggerError(f"poll id mismatch: stored as '{previous.get('poll_id')}'")
    source = str(body.get("source") or poll_sources.DEFAULT_SOURCE).strip().lower() \
        or poll_sources.DEFAULT_SOURCE
    try:
        spec = poll_sources.resolve(source)
    except ValueError as exc:
        raise TriggerError(str(exc)) from None
    extras: dict = {}
    if spec is not None:
        extras = spec.validate(body) or {}
        if not isinstance(extras, dict):
            raise TriggerError(f"poll source '{source}' returned an invalid fetch spec")
        extras = {key: value for key, value in extras.items()
                  if key not in SOURCE_LOCKED_KEYS}
    url = str(extras.pop("url", body.get("url")) or "").strip()
    if source == poll_sources.DEFAULT_SOURCE and not URL_PATTERN.fullmatch(url):
        raise TriggerError("url must be an http(s) URL")
    method = str(extras.pop("method", body.get("method")) or "GET").upper()
    if method not in METHODS:
        raise TriggerError(f"method must be one of: {', '.join(METHODS)}")
    headers = extras.pop("headers", body.get("headers")) or {}
    if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, (str, int, float))
            for key, value in headers.items()):
        raise TriggerError("headers must be an object of string values")
    body_spec = extras.pop("body", body.get("body"))
    if body_spec is not None and not isinstance(body_spec, (str, dict, list)):
        raise TriggerError("body must be a string or a JSON object/array")
    cursor_mode = str(extras.pop("cursor_mode", body.get("cursor_mode"))
                      or "watermark").strip().lower()
    if cursor_mode not in CURSOR_MODES:
        raise TriggerError(f"cursor_mode must be one of: {', '.join(CURSOR_MODES)}")
    max_items = extras.pop("max_items", body.get("max_items", 25))
    try:
        max_items = int(max_items)
    except (TypeError, ValueError):
        raise TriggerError("max_items must be a number") from None
    if not 1 <= max_items <= MAX_ITEMS_CAP:
        raise TriggerError(f"max_items must be 1-{MAX_ITEMS_CAP}")
    # dedupe_ttl_days: how long an emitted item id stays in the seen-set
    # that stops ``next_cursor`` pages from re-running it (seen.TTL_DAYS is
    # the default window; 1-365 keeps a typo from pinning or unpinning
    # history forever).
    dedupe_ttl_days = extras.pop("dedupe_ttl_days", body.get("dedupe_ttl_days"))
    if dedupe_ttl_days is None:
        dedupe_ttl_days = seen.TTL_DAYS
    try:
        dedupe_ttl_days = int(dedupe_ttl_days)
    except (TypeError, ValueError):
        raise TriggerError("dedupe_ttl_days must be a number") from None
    if not 1 <= dedupe_ttl_days <= 365:
        raise TriggerError("dedupe_ttl_days must be 1-365")
    actions = resolve_actions(body)
    list_path = extras.pop("list_path", body.get("list_path"))
    if list_path is not None and str(list_path).strip():
        list_path = _validate_path(list_path, "list_path")
    # cursor_path parses an HTTP response's continuation cursor; a poll
    # source returns its next cursor from fetch() directly, so it only
    # applies to the classic http fetch.
    cursor_path = extras.pop("cursor_path", body.get("cursor_path"))
    if cursor_mode == "next_cursor" and spec is None:
        cursor_path = _validate_path(cursor_path, "cursor_path")
    else:
        cursor_path = ""
    return {
        "poll_id": name,
        "source": source,
        "expression": validate_expression(body.get("expression")),
        "url": url,
        "method": method,
        "headers": {str(key): str(value) for key, value in headers.items()},
        "body": body_spec,
        "list_path": list_path or "",
        "id_path": _validate_path(extras.pop("id_path", body.get("id_path")),
                                  "id_path") if cursor_mode == "watermark" else "",
        "connection_id": str(extras.pop("connection_id", body.get("connection_id"))
                             or "").strip(),
        "cursor_mode": cursor_mode,
        "cursor_path": cursor_path,
        "cursor_query": str(extras.pop("cursor_query", body.get("cursor_query"))
                            or "cursor").strip(),
        **extras,
        "max_items": max_items,
        "dedupe_ttl_days": dedupe_ttl_days,
        "description": str(body.get("description") or "")[:200],
        "actions": actions or [],
        "enabled": bool(body.get("enabled", True)),
        "created_by": previous.get("created_by") or str(operator or ""),
        "created_at": previous.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def get_table(table_ref=None):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(TABLE_ENV)
    if not name:
        raise TriggerError("poll triggers are not configured")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def cursor_table(table_ref=None):
    if table_ref is not None:
        return table_ref
    name = os.environ.get(CURSOR_TABLE_ENV)
    if not name:
        raise TriggerError("poll triggers need the cursors table")
    import boto3

    return boto3.resource("dynamodb").Table(name)


def worker_arn():
    arn = (os.environ.get(WORKER_ARN_ENV) or "").strip()
    if not arn:
        raise TriggerError("the worker function ARN is not configured for poll triggers")
    return arn


def _events(events_client=None):
    if events_client is not None:
        return events_client
    import boto3

    return boto3.client("events")


def _decode_numbers(value):
    """DynamoDB's resource API returns Decimals; engine params must stay JSON-safe."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _decode_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_numbers(item) for item in value]
    return value


def _scan_all(table):
    """Read every scan page: a single Limit=200 scan silently dropped
    trigger #201 and beyond — it would stop firing with no error anywhere.
    Same walk as published_workflows._scan_all (the managed-store loader)."""
    items, start = [], None
    while True:
        kwargs = {"Limit": SCAN_LIMIT}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def load_items(table_ref=None):
    return sorted(
        ({key: _decode_numbers(value) for key, value in item.items()}
         for item in _scan_all(get_table(table_ref))),
        key=lambda item: item.get("poll_id", ""),
    )


def get_item(name, table_ref=None):
    name = str(name or "").strip().lower()
    item = get_table(table_ref).get_item(Key={"poll_id": name}).get("Item")
    return {key: _decode_numbers(value) for key, value in item.items()} if item else None


def sync_rule(item, *, events_client=None, target_arn=None):
    """Create or update the EventBridge rule and its worker target."""
    events = _events(events_client)
    rule = rule_name(item["poll_id"])
    state = "ENABLED" if item.get("enabled", True) else "DISABLED"
    events.put_rule(
        Name=rule,
        ScheduleExpression=item["expression"],
        State=state,
        Description=item.get("description") or "Dapier poll trigger",
    )
    events.put_targets(
        Rule=rule,
        Targets=[{
            "Id": TARGET_ID,
            "Arn": target_arn if target_arn is not None else worker_arn(),
            "Input": json.dumps({"trigger": "poll", "poll_id": item["poll_id"]}),
        }],
    )


def remove_rule(poll_id, *, events_client=None):
    """Best-effort teardown of the rule; a missing rule is already gone."""
    from botocore.exceptions import ClientError

    events = _events(events_client)
    rule = rule_name(poll_id)
    try:
        events.remove_targets(Rule=rule, Ids=[TARGET_ID], Force=True)
        events.delete_rule(Name=rule, Force=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise


def _resolve_path(value, path):
    for part in [segment for segment in str(path or "").split(".") if segment]:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _sort_key(value):
    """Watermarks compare numerically when both sides are numeric, else as text."""
    try:
        return (0, float(value))
    except (TypeError, ValueError):
        return (1, str(value))


def get_cursor(name, table=None):
    item = (table or cursor_table()).get_item(Key={"cursor_id": f"{CURSOR_KEY_PREFIX}{name}"}).get("Item")
    return item.get("cursor") if item else None


def put_cursor(name, cursor, table=None):
    (table or cursor_table()).put_item(Item={
        "cursor_id": f"{CURSOR_KEY_PREFIX}{name}",
        "cursor": str(cursor),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })


def delete_cursor(name, table=None):
    (table or cursor_table()).delete_item(Key={"cursor_id": f"{CURSOR_KEY_PREFIX}{name}"})


def _bearer_token(connection_id):
    """The fetch's bearer token: refreshed for OAuth connections, stored for
    static-token providers (whose credentials never expire)."""
    if not connection_id:
        return None
    from ..engine.actions import base

    connection = base._connected_connection(connection_id)
    if connection.get("provider"):
        from ..connections import tokens

        try:
            token, _info = tokens.get_access_token(connection)
        except tokens.TokenError as exc:
            raise TriggerError(f"connection {connection_id} has no usable token: {exc}") from None
        return token
    from ..connections import credentials

    secret = credentials.get_credential(connection["credential_id"])
    token = secret.get("token") or secret.get("access_token")
    if not token:
        raise TriggerError(f"connection {connection_id} has no stored token")
    return token


def _with_cursor_query(url, query_param, cursor):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query[query_param] = [str(cursor)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _transport_call(method, url, *, headers, body, timeout, transport=None):
    if transport is not None:
        return transport(method, url, headers=headers, body=body, timeout=timeout)
    import urllib.request

    payload = json.dumps(body).encode() if isinstance(body, (dict, list)) else (
        str(body).encode() if body else None)
    if payload is not None:
        headers = {**headers, "content-type": headers.get("content-type") or "application/json"}
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def fetch_page(item, *, cursor=None, transport=None):
    """One API page as a list of raw items (the ``list_path`` selection)."""
    return fetch_page_response(item, cursor=cursor, transport=transport)[0]


def fetch_page_response(item, *, cursor=None, transport=None):
    """One API page as ``(items, next_cursor)``.

    ``next_cursor`` — the response's continuation cursor (``cursor_path``),
    str()-ready for the next fire's ``cursor_query``, or None when the
    response carries none (or the poll is not ``next_cursor`` mode).
    """
    spec = poll_sources.stored_source(item)
    if spec is not None:
        return spec.fetch(item, cursor)
    headers = dict(item.get("headers") or {})
    token = _bearer_token(item.get("connection_id"))
    if token:
        headers.setdefault("authorization", f"Bearer {token}")
    body = item.get("body")
    url = item["url"]
    if item.get("cursor_mode") == "next_cursor" and cursor is not None:
        url = _with_cursor_query(url, item.get("cursor_query") or "cursor", cursor)
    status, raw = _transport_call(
        item.get("method", "GET"), url, headers=headers, body=body,
        timeout=15, transport=transport,
    )
    if status >= 300:
        raise RuntimeError(f"poll fetch returned HTTP {status}")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError("poll fetch did not return JSON") from None
    items = _resolve_path(payload, item.get("list_path")) if item.get("list_path") else payload
    if items is None:
        items = []
    if not isinstance(items, list):
        raise RuntimeError(f"poll list_path '{item.get('list_path')}' did not select a list")
    next_cursor = None
    if item.get("cursor_mode") == "next_cursor":
        found = _resolve_path(payload, item.get("cursor_path"))
        if found is not None:
            next_cursor = str(found)
    return items, next_cursor


def _stable_event_id(name, item_id):
    """The stable event id for one poll item: the trigger's name plus a hash
    of the item id, so the same item always publishes under one id."""
    return f"{name}-{hashlib.sha256(str(item_id).encode()).hexdigest()[:16]}"


def event_for(item, raw):
    """The dapier event for one new list item (stable id, per-item dedupe)."""
    name = item["poll_id"]
    spec = poll_sources.stored_source(item)
    data = raw if isinstance(raw, dict) else {"item": raw}
    item_id = _resolve_path(data, item.get("id_path")) if item.get("id_path") else None
    if item_id is None:
        item_id = data.get("id")
    if item_id is None:
        raise ValueError(f"poll '{name}': item has no value at id_path '{item.get('id_path')}'")
    item_id = str(item_id)
    stable = _stable_event_id(name, item_id)
    return {
        "schema_version": "1.0",
        "id": stable,
        "correlation_id": stable,
        "connector": spec.connector if spec else POLL_CONNECTOR,
        "event": spec.event if spec else POLL_EVENT,
        "source": name,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": {**data, "poll": name, "item_id": item_id},
    }


def fire(name, *, table_ref=None, cursor_table_ref=None, transport=None):
    """One scheduled fire: fetch, emit each new item, advance the cursor.

    Emits at most ``max_items`` per fire; a failed item stops the fire with
    the cursor left below it, so it (and everything after) retries next time.

    Cursor handling follows the mode. Watermark fires advance the stored
    id per emitted item, ending at the newest. ``next_cursor`` fires park
    the response's continuation cursor (``cursor_path``) only once the page
    was processed down to nothing — a page larger than ``max_items`` (or a
    failed item) keeps the cursor so the page refetches and the remainder
    runs then.

    ``next_cursor`` polls additionally skip items whose stable id was
    already published (a TTL-bounded seen-set beside the cursor): providers
    recycle pages — the continuation cursor advances but items can repeat —
    and a repeated item must not re-run its workflows. Like the cursor, an
    item is marked seen only after its run succeeded, so a failure retries.
    """
    item = get_item(name, table_ref=table_ref)
    if not item:
        logger.warning("poll trigger '%s' is gone; skipping", name)
        return {"poll": name, "fired": 0, "skipped": "missing"}
    if not item.get("enabled", True):
        return {"poll": name, "fired": 0, "skipped": "disabled"}

    from ..engine import execute
    from ..engine.notify import notify_failure
    from ..engine.worker import _is_pending, _mark_completed, _release_action
    from . import inbox, seen

    watermark = item.get("cursor_mode") == "watermark"
    cursor = get_cursor(name, table=cursor_table_ref)
    items, next_cursor = fetch_page_response(item, cursor=cursor, transport=transport)
    if watermark:
        candidates = [raw for raw in items if _raw_id(item, raw) is not None]
        # No stored cursor (first fire) means nothing was emitted yet, so
        # every listed item is new — an ISO-timestamp watermark would
        # otherwise compare "less than" the unseeded cursor forever.
        if cursor is not None:
            candidates = [raw for raw in candidates
                          if _sort_key(_raw_id(item, raw)) > _sort_key(cursor)]
        # Oldest first, so the cursor ends at the newest emitted item.
        items = sorted(candidates, key=lambda raw: _sort_key(_raw_id(item, raw)))
    seen_ids = None
    skipped_seen = 0
    ttl_days = item.get("dedupe_ttl_days") or seen.TTL_DAYS
    if not watermark:
        seen_ids = seen.load(f"poll#{name}", table_ref=cursor_table_ref)
    fired = 0
    failed = False
    leftover_fresh = False
    for raw in items:
        if seen_ids is not None:
            # The seen key is the stable event id, decided by the item's own
            # id — a recycled page is recognized without building the event.
            raw_id = _raw_id(item, raw)
            if raw_id is not None and _stable_event_id(name, raw_id) in seen_ids:
                skipped_seen += 1
                continue
        if fired >= item.get("max_items", 25):
            # Budget spent, but this item is new: remember that the page was
            # not drained, so the continuation cursor is not parked over it.
            leftover_fresh = True
            continue
        event = event_for(item, raw)
        inbox_id = inbox.record(event)
        try:
            matched = execute(
                event,
                before_action=_is_pending,
                after_action=_mark_completed,
                on_action_error=_release_action,
            )
        except Exception as exc:
            inbox.complete(inbox_id, None, error=exc)
            notify_failure(exc, event)
            logger.exception("poll trigger '%s' item failed", name, extra={"item_id": event["data"]["item_id"]})
            failed = True
            break
        inbox.complete(inbox_id, matched)
        if watermark:
            put_cursor(name, event["data"]["item_id"], table=cursor_table_ref)
        if seen_ids is not None:
            seen_ids = seen.remember(f"poll#{name}", event["id"], seen_ids,
                                     table_ref=cursor_table_ref, ttl_days=ttl_days)
        fired += 1
    if (not watermark and not failed and not leftover_fresh
            and next_cursor is not None):
        # Every item on the page was emitted, or skipped as already seen:
        # park the provider's continuation cursor so the next fire asks for
        # what comes after this page, not the page again. (Without the
        # seen-set, skipping already-seen items over the budget could starve
        # fresh ones behind them forever — hence leftover_fresh.)
        put_cursor(name, next_cursor, table=cursor_table_ref)
    result = {"poll": name, "fired": fired}
    if skipped_seen:
        result["skipped_seen"] = skipped_seen
    return result


def _raw_id(item, raw):
    data = raw if isinstance(raw, dict) else {"item": raw}
    item_id = _resolve_path(data, item.get("id_path")) if item.get("id_path") else None
    if item_id is None:
        item_id = data.get("id")
    return item_id


def workflow_for(item):
    """The engine workflow for a stored poll trigger."""
    actions = item.get("actions") or []
    spec = poll_sources.stored_source(item)
    return {
        "id": workflow_id_for(item),
        "enabled": True,
        "trigger": {
            "connector": spec.connector if spec else POLL_CONNECTOR,
            "event": spec.event if spec else POLL_EVENT,
            "filters": {"poll": {"equals": item["poll_id"]}},
        },
        "actions": actions,
    }


def load_workflows(table_ref=None):
    return [
        workflow for workflow in
        (workflow_for(item) for item in load_items(table_ref=table_ref) if item.get("enabled", True))
        if workflow is not None
    ]


def public_view(item, cursor=None):
    spec = poll_sources.stored_source(item) if item.get("source") else None
    view = spec.view(item) if spec is not None and spec.view else {}
    return {
        "poll_id": item.get("poll_id"),
        "source": item.get("source") or poll_sources.DEFAULT_SOURCE,
        **view,
        "expression": item.get("expression"),
        "rule": rule_name(item.get("poll_id", "")),
        "url": item.get("url"),
        "method": item.get("method"),
        "headers": item.get("headers"),
        "body": item.get("body"),
        "list_path": item.get("list_path"),
        "id_path": item.get("id_path"),
        "connection_id": item.get("connection_id"),
        "cursor_mode": item.get("cursor_mode"),
        "cursor_path": item.get("cursor_path"),
        "cursor_query": item.get("cursor_query"),
        "cursor": cursor,
        "max_items": item.get("max_items"),
        "dedupe_ttl_days": item.get("dedupe_ttl_days"),
        "description": item.get("description"),
        "actions": item.get("actions"),
        "flow": item.get("flow"),
        "enabled": item.get("enabled", True),
        "created_by": item.get("created_by"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
    }


def api_list(table_ref=None, cursor_table_ref=None):
    return 200, {
        "polls": [
            public_view(item, cursor=get_cursor(item["poll_id"], table=cursor_table_ref))
            for item in load_items(table_ref=table_ref)
        ],
    }


def api_save(body, operator, *, table_ref=None, cursor_table_ref=None, events_client=None, target_arn=None):
    name = str((body or {}).get("name") or "").strip().lower()
    previous = get_item(name, table_ref=table_ref)
    item = build_item(body, operator, previous=previous)
    # Sync the rule before storing: a failed Events call must not leave a
    # stored trigger whose schedule never fires.
    sync_rule(item, events_client=events_client, target_arn=target_arn)
    get_table(table_ref).put_item(Item=item)
    return 200, {"created": previous is None,
                 **public_view(item, cursor=get_cursor(name, table=cursor_table_ref))}


def api_delete(name, operator, *, table_ref=None, cursor_table_ref=None, events_client=None):
    name = str(name or "").strip().lower()
    item = get_item(name, table_ref=table_ref)
    if not item:
        raise TriggerError(f"no poll trigger named '{name}'")
    remove_rule(item["poll_id"], events_client=events_client)
    get_table(table_ref).delete_item(Key={"poll_id": item["poll_id"]})
    delete_cursor(name, table=cursor_table_ref)
    # The seen-set goes too: a recreated trigger starts clean instead of
    # suppressing its old items for the rest of the dedupe window.
    seen.drop(f"poll#{name}", table_ref=cursor_table_ref)
    return 200, {"ok": True, "poll_id": item["poll_id"]}
