"""Audit records for connection lifecycle events.

Audit items contain IDs and outcomes only: connection IDs, actor subjects,
agent names, actions, outcomes, and redacted error summaries. Tokens, auth
codes, client secrets, and full authorization URLs must never reach this
module; error text is scrubbed with :func:`oauth_providers.redact` anyway.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone

from .oauth_providers import redact

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
