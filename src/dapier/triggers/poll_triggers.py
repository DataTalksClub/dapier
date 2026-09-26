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
  query parameter on the next fire, and every returned page counts as new.

The cursor advances only after an item's workflows ran, so a failed action
stops the fire and the item is retried on the next one.
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .email_triggers import (
    NAME_PATTERN, TriggerError, flow_catalog, resolve_actions_flow,
)
from .schedule_triggers import validate_expression

logger = logging.getLogger(__name__)

TABLE_ENV = "POLL_TRIGGERS_TABLE"
CURSOR_TABLE_ENV = "CURSORS_TABLE"
WORKER_ARN_ENV = "WORKER_FUNCTION_ARN"
RULE_PREFIX = "dapier-poll-"
TARGET_ID = "dapier-worker"
POLL_CONNECTOR = "poll"
POLL_EVENT = "item.new"
CURSOR_KEY_PREFIX = "poll#"

CURSOR_MODES = ("watermark", "next_cursor")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MAX_ITEMS_CAP = 100
URL_PATTERN = re.compile(r"^https?://\S+$")


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
    url = str(body.get("url") or "").strip()
    if not URL_PATTERN.fullmatch(url):
        raise TriggerError("url must be an http(s) URL")
    method = str(body.get("method") or "GET").upper()
    if method not in METHODS:
        raise TriggerError(f"method must be one of: {', '.join(METHODS)}")
    headers = body.get("headers") or {}
    if not isinstance(headers, dict) or not all(
            isinstance(key, str) and isinstance(value, (str, int, float))
            for key, value in headers.items()):
        raise TriggerError("headers must be an object of string values")
    body_spec = body.get("body")
    if body_spec is not None and not isinstance(body_spec, (str, dict, list)):
        raise TriggerError("body must be a string or a JSON object/array")
    cursor_mode = str(body.get("cursor_mode") or "watermark").strip().lower()
    if cursor_mode not in CURSOR_MODES:
        raise TriggerError(f"cursor_mode must be one of: {', '.join(CURSOR_MODES)}")
    max_items = body.get("max_items", 25)
    try:
        max_items = int(max_items)
    except (TypeError, ValueError):
        raise TriggerError("max_items must be a number") from None
    if not 1 <= max_items <= MAX_ITEMS_CAP:
        raise TriggerError(f"max_items must be 1-{MAX_ITEMS_CAP}")
    actions, flow = resolve_actions_flow(body)
    list_path = body.get("list_path")
    if list_path is not None and str(list_path).strip():
        list_path = _validate_path(list_path, "list_path")
    return {
        "poll_id": name,
        "expression": validate_expression(body.get("expression")),
        "url": url,
        "method": method,
        "headers": {str(key): str(value) for key, value in headers.items()},
        "body": body_spec,
        "list_path": list_path or "",
        "id_path": _validate_path(body.get("id_path"), "id_path") if cursor_mode == "watermark" else "",
        "connection_id": str(body.get("connection_id") or "").strip(),
        "cursor_mode": cursor_mode,
        "cursor_path": _validate_path(body.get("cursor_path"), "cursor_path") if cursor_mode == "next_cursor" else "",
        "cursor_query": str(body.get("cursor_query") or "cursor").strip(),
        "max_items": max_items,
        "description": str(body.get("description") or "")[:200],
        "actions": actions or [],
        "flow": flow,
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


def load_items(table_ref=None):
    items = get_table(table_ref).scan(Limit=200).get("Items", [])
    return sorted(
        ({key: _decode_numbers(value) for key, value in item.items()} for item in items),
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
        return []
    if not isinstance(items, list):
        raise RuntimeError(f"poll list_path '{item.get('list_path')}' did not select a list")
    return items


def event_for(item, raw):
    """The dapier event for one new list item (stable id, per-item dedupe)."""
    name = item["poll_id"]
    data = raw if isinstance(raw, dict) else {"item": raw}
    item_id = _resolve_path(data, item.get("id_path"))
    if item_id is None and not item.get("id_path"):
        item_id = data.get("id")
    if item_id is None:
        raise ValueError(f"poll '{name}': item has no value at id_path '{item.get('id_path')}'")
    item_id = str(item_id)
    stable = f"{name}-{hashlib.sha256(item_id.encode()).hexdigest()[:16]}"
    return {
        "schema_version": "1.0",
        "id": stable,
        "correlation_id": stable,
        "connector": POLL_CONNECTOR,
        "event": POLL_EVENT,
        "source": name,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "data": {**data, "poll": name, "item_id": item_id},
    }


def fire(name, *, table_ref=None, cursor_table_ref=None, transport=None):
    """One scheduled fire: fetch, emit each new item, advance the cursor.

    Emits at most ``max_items`` per fire; a failed item stops the fire with
    the cursor left below it, so it (and everything after) retries next time.
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

    cursor = get_cursor(name, table=cursor_table_ref)
    items = fetch_page(item, cursor=cursor, transport=transport)
    if item.get("cursor_mode") == "watermark":
        candidates = [raw for raw in items if _raw_id(item, raw) is not None]
        # No stored cursor (first fire) means nothing was emitted yet, so
        # every listed item is new — an ISO-timestamp watermark would
        # otherwise compare "less than" the unseeded cursor forever.
        if cursor is not None:
            candidates = [raw for raw in candidates
                          if _sort_key(_raw_id(item, raw)) > _sort_key(cursor)]
        # Oldest first, so the cursor ends at the newest emitted item.
        items = sorted(candidates, key=lambda raw: _sort_key(_raw_id(item, raw)))
    fired = 0
    for raw in items[:item.get("max_items", 25)]:
        event = event_for(item, raw)
        try:
            execute(
                event,
                before_action=_is_pending,
                after_action=_mark_completed,
                on_action_error=_release_action,
            )
        except Exception as exc:
            notify_failure(exc, event)
            logger.exception("poll trigger '%s' item failed", name, extra={"item_id": event["data"]["item_id"]})
            break
        put_cursor(name, event["data"]["item_id"], table=cursor_table_ref)
        fired += 1
    return {"poll": name, "fired": fired}


def _raw_id(item, raw):
    data = raw if isinstance(raw, dict) else {"item": raw}
    item_id = _resolve_path(data, item.get("id_path")) if item.get("id_path") else None
    if item_id is None:
        item_id = data.get("id")
    return item_id


def workflow_for(item):
    """The engine workflow for a stored poll trigger, or None when its flow is gone."""
    actions = item.get("actions") or []
    flow = str(item.get("flow") or "").strip()
    if flow:
        from ..engine import matching

        actions = matching.flow_actions(flow)
        if actions is None:
            logger.warning("poll trigger '%s' binds undefined flow '%s'; skipped",
                           item.get("poll_id"), flow)
            return None
    return {
        "id": workflow_id_for(item),
        "enabled": True,
        "trigger": {
            "connector": POLL_CONNECTOR,
            "event": POLL_EVENT,
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
    return {
        "poll_id": item.get("poll_id"),
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
        "flows": flow_catalog(),
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
    return 200, {"ok": True, "poll_id": item["poll_id"]}
