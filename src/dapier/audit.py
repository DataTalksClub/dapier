"""Audit records for connection lifecycle events.

Audit items contain IDs and outcomes only: connection IDs, actor subjects,
agent names, actions, outcomes, and redacted error summaries. Tokens, auth
codes, client secrets, and full authorization URLs must never reach this
module; error text is scrubbed with :func:`oauth_providers.redact` anyway.
"""

import base64
import csv
import io
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from .connections.providers.oauth_providers import redact

logger = logging.getLogger(__name__)

RETENTION_DAYS = 90

CONNECT = "connect"
CALLBACK = "callback"
TOKEN = "token"
REFRESH = "refresh"
ROTATE = "rotate"
REVOKE = "revoke"
GRANT = "grant"
IMPORT = "import"
CONFIG = "config"
API_TOKEN = "api-token"
DEVICE_LOGIN = "device-login"


def audit_table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["AUDIT_TABLE"])


def record(table, *, connection_id, action, actor_subject, outcome,
           agent=None, error=None, now=None):
    """Write one audit item. Best-effort: storage failures are logged."""
    now = int(now if now is not None else time.time())
    item = {
        "audit_id": f"{connection_id}#{now}#{uuid.uuid4().hex[:12]}",
        "connection_id": connection_id,
        "action": action,
        "actor_subject": actor_subject,
        "agent": agent,
        "outcome": outcome,
        "timestamp": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "expires_at": now + RETENTION_DAYS * 86400,
    }
    if error is not None:
        item["error"] = str(redact(str(error)))[:500]
    try:
        table.put_item(Item=item)
    except Exception:
        logger.warning("audit write failed", extra={"audit_id": item["audit_id"]})
    return item


def recent(table, limit=100):
    return table.scan(Limit=limit).get("Items", [])


# The fields the read surface returns: exactly what record() writes, minus
# the internal expires_at bookkeeping. audit_id stays in the projection — it
# is the row's address, and paging and CSV export key rows on it. Anything
# else that ever lands in the table stays unreachable from the API.
AUDIT_FIELDS = ("connection_id", "action", "actor_subject", "agent",
                "outcome", "timestamp", "error", "audit_id")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_SCAN_PAGES = 4
EXPORT_DEFAULT_ROWS = 1000
EXPORT_MAX_ROWS = 5000

# CSV export column order; stable so downstream parsers can rely on it.
CSV_COLUMNS = AUDIT_FIELDS

# queryStringParameters → api_recent/api_export filter kwargs, shared by the
# admin and agent wrappers so both surfaces accept the same spellings.
QUERY_FILTERS = {
    "connection": "connection_id",
    "action": "action",
    "actor": "actor",
    "agent": "agent",
    "outcome": "outcome",
    "since": "since",
    "before": "before",
    "q": "query",
}


def filters_from_query(query):
    """queryStringParameters as api_recent/api_export kwargs, blanks dropped."""
    picked = {}
    for key, kwarg in QUERY_FILTERS.items():
        value = str(query.get(key) or "").strip()
        if value:
            picked[kwarg] = value
    return picked


def _public(item):
    return {key: item[key] for key in AUDIT_FIELDS if item.get(key) is not None}


def _parse_ts(value):
    """An aware UTC datetime for an ISO date/datetime, else None."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _sort_key(item):
    # Timestamps carry second precision (record() floors to int(time.time())),
    # so rows tie often; audit_id's trailing uuid breaks the tie.
    return (str(item.get("timestamp") or ""), str(item.get("audit_id") or ""))


def _matches(item, *, connection_id=None, action=None, actor=None, agent=None,
             outcome=None, since=None, before=None, query=None):
    """Whether one stored item passes every filter.

    Connection/actor/agent/outcome match exactly; ``action`` matches exactly
    or as a family when the filter ends with a dot (``workflow.`` covers
    save/toggle/test/...); the window is [since, before) with the runs list's
    parse-then-fall-back-to-string comparison; ``query`` is a case-insensitive
    substring across the display fields.
    """
    if connection_id and str(item.get("connection_id") or "") != connection_id:
        return False
    if actor and str(item.get("actor_subject") or "") != actor:
        return False
    if agent and str(item.get("agent") or "") != agent:
        return False
    if outcome and str(item.get("outcome") or "") != outcome:
        return False
    wanted_action = str(action or "").strip().lower()
    if wanted_action:
        own_action = str(item.get("action") or "").strip().lower()
        if own_action != wanted_action and not (
                wanted_action.endswith(".") and own_action.startswith(wanted_action)):
            return False
    if since or before:
        stamp = str(item.get("timestamp") or "")
        moment, since_at, before_at = _parse_ts(stamp), _parse_ts(since), _parse_ts(before)
        if moment is None or (since_at is None and since) or (before_at is None and before):
            if since and stamp < since:
                return False
            if before and stamp >= before:
                return False
        else:
            if since_at and moment < since_at:
                return False
            if before_at and moment >= before_at:
                return False
    if query:
        haystack = " ".join(
            str(item.get(field) or "") for field in
            ("action", "connection_id", "actor_subject", "agent", "outcome", "error")
        ).lower()
        if str(query).strip().lower() not in haystack:
            return False
    return True


def _encode_token(item):
    payload = json.dumps({"t": item.get("timestamp") or "",
                          "i": item.get("audit_id") or ""})
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_token(text):
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        key = (str(data.get("t") or ""), str(data.get("i") or ""))
    except (ValueError, TypeError):
        return None
    return key if key != ("", "") else None


def _reader_table():
    if not os.environ.get("AUDIT_TABLE", ""):
        return None
    try:
        return audit_table()
    except KeyError:
        return None


def _scan_window(table, page_size, max_pages):
    """A bounded scan walk: pages until the table ends or ``max_pages``."""
    items = []
    kwargs = {"Limit": page_size}
    for _ in range(max_pages):
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def _collect(table, filters, *, page_size, max_pages):
    """Filtered events, newest first, from a bounded scan.

    Unavailable or failing storage degrades to an empty trail — a read must
    never fail a page.
    """
    if table is None:
        return []
    try:
        items = _scan_window(table, page_size, max_pages)
    except Exception:  # noqa: BLE001 — audit reads degrade, never block
        return []
    events = [_public(item) for item in items if _matches(item, **filters)]
    events.sort(key=_sort_key, reverse=True)
    return events


def api_recent(action=None, limit=DEFAULT_LIMIT, *, connection_id=None,
               actor=None, agent=None, outcome=None, since=None, before=None,
               query=None, next_token=None, table=None):
    """The audit trail for the read surface: ``(status, payload)``.

    Newest first. Filters: exact ``action`` or an action family via a
    trailing dot (``workflow.``), connection, actor, agent, outcome, the
    ``[since, before)`` window, and free-text ``query`` over the display
    fields. ``paging.next`` carries an opaque token; the follow-up call
    passes it back as ``next_token`` and the window starts strictly after
    that row, so filters and paging compose. The scanned window is bounded
    like every list call — a filter matching only events older than the
    window returns nothing rather than walking the whole table.
    """
    try:
        limit = max(1, min(int(limit), MAX_LIMIT))
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    token_key = _decode_token(next_token) if next_token else None
    if next_token and token_key is None:
        return 400, {"error": "Invalid page token"}
    filters = dict(connection_id=connection_id, action=action, actor=actor,
                   agent=agent, outcome=outcome, since=since, before=before,
                   query=query)
    if table is None:
        table = _reader_table()
    # A wider scanned window than the page, so filtered and paged reads still
    # fill their page when matches sit deeper in the table.
    events = _collect(table, filters, page_size=min(max(limit * 12, 300), 1200),
                      max_pages=MAX_SCAN_PAGES)
    if token_key is not None:
        events = [event for event in events if _sort_key(event) < token_key]
    page = events[:limit]
    return 200, {
        "events": page,
        "paging": {
            "next": _encode_token(page[-1]) if len(events) > limit and page else None,
            "limit": limit,
            "filtered": bool(token_key is not None or any(filters.values())),
        },
    }


def to_csv(events):
    """The audit rows as CSV text: one header row, one row per event."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for item in events:
        writer.writerow([item.get(column) or "" for column in CSV_COLUMNS])
    return buffer.getvalue()


def api_export(action=None, connection_id=None, actor=None, agent=None,
               outcome=None, since=None, before=None, query=None,
               max_rows=EXPORT_DEFAULT_ROWS, table=None, now=None):
    """The filtered audit trail as CSV: ``(status, payload)``.

    Same filters as api_recent, newest first, capped at ``max_rows`` rows
    (``truncated`` flags the clip). Returns ``{filename, count, truncated,
    csv}`` — the caller decides delivery (CLI file write, console download).
    """
    try:
        max_rows = max(1, min(int(max_rows), EXPORT_MAX_ROWS))
    except (TypeError, ValueError):
        max_rows = EXPORT_DEFAULT_ROWS
    filters = dict(connection_id=connection_id, action=action, actor=actor,
                   agent=agent, outcome=outcome, since=since, before=before,
                   query=query)
    if table is None:
        table = _reader_table()
    # Enough bounded pages to cover the cap when the table is dense; filtered
    # exports may still clip early — the same bounded-window trade as reads.
    events = _collect(table, filters, page_size=600,
                      max_pages=max(MAX_SCAN_PAGES, -(-max_rows // 600) + 1))
    truncated = len(events) > max_rows
    page = events[:max_rows]
    stamp = datetime.fromtimestamp(int(now if now is not None else time.time()),
                                   timezone.utc)
    return 200, {
        "filename": f"dapier-audit-{stamp:%Y%m%d-%H%M%S}.csv",
        "count": len(page),
        "truncated": truncated,
        "csv": to_csv(page),
    }


def emit(connection_id, action, actor_subject, *, outcome, agent=None, error=None):
    """Best-effort audit write. No-ops when AUDIT_TABLE is not configured."""
    table_name = os.environ.get("AUDIT_TABLE", "")
    if not table_name:
        return None
    try:
        table = audit_table()
    except KeyError:
        return None
    return record(
        table, connection_id=connection_id, action=action,
        actor_subject=actor_subject, agent=agent, outcome=outcome, error=error,
    )
