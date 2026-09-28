"""Operator authorization and per-connection agent grants.

Two layers:

1. Operators administer connections, credentials, and grants. Sign-in goes
   through the DTC identity provider, which only issues datatalks.club
   accounts, so every authenticated account may administer the console.
   ``OPERATOR_SUBJECTS`` / ``OPERATOR_EMAILS`` optionally restrict it to a
   subset of accounts (matched by stable subject or email).
2. Agents use connections only through explicit grants keyed by
   ``(connection_id, subject, agent)`` with an allowed operation
   (``use``, ``connect``, ``admin``). No grant means no access. Grants may
   carry ``expires_at`` for short-lived delegation; headless machine
   identities are not enrolled yet (see docs).
"""

import base64
import json
import os
import re
import time
from datetime import datetime, timezone

AGENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,62}")
OPERATIONS = ("use", "connect", "admin")

# "admin" implies every operation.
_IMPLIED = {"admin": set(OPERATIONS), "connect": {"connect"}, "use": {"use"}}


def _split_env(name):
    return {part.strip() for part in os.environ.get(name, "").split(",") if part.strip()}


def operator_allowlists():
    """Return ``(subjects, emails)``. Email comparison is case-insensitive."""
    subjects = _split_env("OPERATOR_SUBJECTS")
    emails = {address.lower() for address in _split_env("OPERATOR_EMAILS")}
    return subjects, emails


def is_operator(session_payload):
    """True when the session may administer the console.

    DTC sign-in is the operator gate, so any authenticated session qualifies
    unless allowlists narrow it to specific subjects or emails.
    """
    if not session_payload:
        return False
    subjects, emails = operator_allowlists()
    if not subjects and not emails:
        return True
    subject = session_payload.get("subject")
    if subject and str(subject) in subjects:
        return True
    email = session_payload.get("sub")
    return bool(email and str(email).lower() in emails)


def validate_agent(agent):
    agent = str(agent or "").strip().lower()
    if not AGENT_RE.fullmatch(agent):
        raise ValueError("Agent must use lowercase letters, numbers, dashes, or underscores")
    return agent


def validate_operations(operations):
    cleaned = sorted({str(op).strip().lower() for op in operations or []})
    unknown = [op for op in cleaned if op not in OPERATIONS]
    if not cleaned or unknown:
        raise ValueError(f"Operations must be a non-empty subset of {list(OPERATIONS)}")
    return cleaned


def grantee(subject, agent):
    return f"{subject}#{validate_agent(agent)}"


def grants_table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["GRANTS_TABLE"])


def check_grant(table, *, subject, agent, connection_id, operation):
    """True when ``subject`` may perform ``operation`` as ``agent``.

    Denies by default: missing/expired grants, unknown operations, and
    mismatched agent names all return False. Naming an arbitrary agent
    grants nothing unless a grant exists for that exact pair.
    """
    if operation not in OPERATIONS or not subject or not connection_id:
        return False
    try:
        agent = validate_agent(agent)
    except ValueError:
        return False
    item = table.get_item(
        Key={"connection_id": connection_id, "grantee": grantee(subject, agent)}
    ).get("Item")
    if not item:
        return False
    expires_at = item.get("expires_at")
    if expires_at:
        try:
            if int(expires_at) <= int(time.time()):
                return False
        except (TypeError, ValueError):
            return False
    allowed = set()
    for op in item.get("operations") or []:
        allowed |= _IMPLIED.get(str(op), set())
    return operation in allowed


def put_grant(table, *, connection_id, subject, agent, operations, granted_by, expires_at=None):
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "connection_id": connection_id,
        "grantee": grantee(subject, agent),
        "subject": subject,
        "agent": validate_agent(agent),
        "operations": validate_operations(operations),
        "granted_by": granted_by,
        "granted_at": now,
        "updated_at": now,
    }
    if expires_at is not None:
        item["expires_at"] = int(expires_at)
    table.put_item(Item=item)
    return item


def delete_grant(table, *, connection_id, grantee_id):
    table.delete_item(Key={"connection_id": connection_id, "grantee": grantee_id})


def list_grants(table, connection_id=None, limit=None):
    """The grant items, sorted by ``(connection_id, grantee)``.

    A full walk: a single-page scan silently clipped at DynamoDB's page
    limit, so grant #101 was unreachable. ``limit`` (legacy callers) slices
    the sorted result; page tokens go through api_list_grants.
    """
    items = sorted(_scan_all_grants(table, connection_id), key=_grant_sort_key)
    return items[:limit] if limit else items


# The paged list (api_list_grants): the same opaque-token contract as the
# runs list — sorted rows, a next token over the last row's sort key, a
# clamped limit, and a 400 for an unparseable token.
GRANTS_DEFAULT_LIMIT = 100
GRANTS_MAX_LIMIT = 500
GRANTS_SCAN_PAGE = 300
GRANTS_MAX_SCAN_PAGES = 100


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _grant_sort_key(item):
    """Stable (connection_id, grantee) order, so paging is deterministic."""
    return (str(item.get("connection_id") or ""), str(item.get("grantee") or ""))


def encode_paging_token(key):
    """Opaque stateless page token: the last row's (connection_id, grantee)."""
    raw = json.dumps({"c": key[0], "g": key[1]}, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_paging_token(token):
    """The ``(connection_id, grantee)`` sort key behind a page token, or None."""
    text = str(token or "")
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        key = (str(data.get("c") or ""), str(data.get("g") or ""))
    except (ValueError, TypeError):
        return None
    return key or None


def _scan_all_grants(table, connection_id=None):
    """Every grant (or one connection's), walking scan/query pages (bounded).

    Grants are small rows and callers aggregate over the whole set (an
    agent's connections, the console's list), so nothing may silently clip
    at DynamoDB's page limit.
    """
    if connection_id:
        kwargs = {
            "KeyConditionExpression": "connection_id = :connection",
            "ExpressionAttributeValues": {":connection": connection_id},
        }
        pull = table.query
    else:
        kwargs = {"Limit": GRANTS_SCAN_PAGE}
        pull = table.scan
    kwargs.setdefault("Limit", GRANTS_SCAN_PAGE)
    items = []
    for _ in range(GRANTS_MAX_SCAN_PAGES):
        page = pull(**kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


GRANT_PUBLIC_FIELDS = (
    "connection_id", "grantee", "subject", "agent", "operations",
    "granted_by", "granted_at", "updated_at", "expires_at",
)


def public_grant(item):
    return {key: item.get(key) for key in GRANT_PUBLIC_FIELDS}


def api_list_grants(table, connection_id=None, limit=None, next_token=None):
    """The list response: grants plus a ``paging`` block.

    ``next`` carries the last returned row's sort key; the follow-up call
    passes it back as ``next_token`` and the window starts strictly after
    that row. The shape is backward compatible — ``grants`` is unchanged,
    ``paging`` is additive.
    """
    limit = max(1, min(_int(limit) or GRANTS_DEFAULT_LIMIT, GRANTS_MAX_LIMIT))
    token_key = decode_paging_token(next_token) if next_token else None
    if next_token and token_key is None:
        return 400, {"error": "Invalid page token"}
    items = sorted(_scan_all_grants(table, connection_id), key=_grant_sort_key)
    if token_key is not None:
        items = [item for item in items if _grant_sort_key(item) > token_key]
    page = items[:limit]
    more = len(items) > limit
    return 200, {
        "grants": [public_grant(item) for item in page],
        "paging": {
            "next": encode_paging_token(_grant_sort_key(page[-1])) if more and page else None,
            "limit": limit,
        },
    }


def api_save_grant(table, body, *, operator, connections_table):
    """Validate a grant request body and store one grant. Returns ``(status, payload)``.

    Shared by the console (cookie) and CLI (bearer) API layers; audit stays
    with the caller.
    """
    from ..connections import records as connection_model

    connection_id = str(body.get("connection_id", "")).strip().lower()
    subject = str(body.get("subject", "")).strip()
    if not connection_id or not subject:
        return 400, {"error": "Connection ID and subject are required"}
    if not connection_model.get_connection(connections_table, connection_id):
        return 404, {"error": "Connection not found"}
    try:
        item = put_grant(
            table,
            connection_id=connection_id,
            subject=subject,
            agent=body.get("agent", ""),
            operations=body.get("operations", []),
            granted_by=operator,
            expires_at=body.get("expires_at"),
        )
    except ValueError as exc:
        return 400, {"error": str(exc)}
    return 200, item


def api_delete_grant(table, connection_id, grantee_id):
    """Revoke one grant by ``(connection_id, grantee)``. Returns ``(status, payload)``."""
    connection_id = str(connection_id or "").strip().lower()
    grantee_id = str(grantee_id or "").strip()
    if not connection_id or not grantee_id:
        return 400, {"error": "Connection ID and grantee are required"}
    delete_grant(table, connection_id=connection_id, grantee_id=grantee_id)
    return 200, {"ok": True}
