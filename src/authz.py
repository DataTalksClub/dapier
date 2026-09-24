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


def list_grants(table, connection_id=None, limit=100):
    if connection_id:
        return table.query(
            KeyConditionExpression="connection_id = :connection",
            ExpressionAttributeValues={":connection": connection_id},
            Limit=limit,
        ).get("Items", [])
    return table.scan(Limit=limit).get("Items", [])
