"""Operator-issued API tokens for headless API consumers.

A Dapier API token is a long-lived bearer credential (``dap_…``) that
 authenticates agent-API calls as a machine principal with the subject
``token:<token_id>``, bound to exactly one agent name. What that principal
may do is governed entirely by the ordinary grants table, so access stays
explicit: issue a token, grant it (connection, agent, operations), revoke
it — the same controls as a human subject, visible in the console and the
CLI.

Only the SHA-256 hash of a token is stored; the plaintext is returned once
at creation. DTC ID-token authentication is untouched: a bearer value
without the ``dap_`` prefix never reaches this module, and API-token
callers never qualify as operators.
"""

import base64
import hashlib
import os
import time
from datetime import datetime, timezone

from . import authz

PREFIX = "dap_"


def table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["API_TOKENS_TABLE"])


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def _new_secret():
    return PREFIX + base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def subject_for(token_id):
    return f"token:{token_id}"


def create(body, operator, table_ref=None):
    """Validate a create request and store one token. Returns ``(status, payload)``.

    The payload carries the plaintext ``token`` exactly once; every other
    view works from the stored hash. IDs are globally unique, including
    revoked tokens, so ``revoke`` can never grab the wrong item.
    """
    try:
        token_id = authz.validate_agent(body.get("token_id") or body.get("name", ""))
        agent = authz.validate_agent(body.get("agent", ""))
    except ValueError as exc:
        return 400, {"error": str(exc)}
    existing = {item.get("token_id") for item in list_all(table_ref)}
    if token_id in existing:
        return 409, {"error": f"An API token named {token_id} already exists"}
    secret = _new_secret()
    item = {
        "token_id": token_id,
        "token_hash": _hash(secret),
        "token_prefix": secret[:12],
        "agent": agent,
        "subject": subject_for(token_id),
        "created_by": operator,
        "created_at": _now(),
    }
    (table_ref or table()).put_item(Item=item)
    return 200, {**public_view(item), "token": secret}


def verify(bearer, table_ref=None):
    """Return the token item for a presented bearer value, else None.

    Revoked tokens stop working immediately: the hash lookup fails only on
    unknown values, so the revocation check is part of the item read.
    """
    if not bearer or not bearer.startswith(PREFIX):
        return None
    try:
        item = (table_ref or table()).get_item(
            Key={"token_hash": _hash(bearer)},
        ).get("Item")
    except Exception:
        return None
    if not item or item.get("revoked_at"):
        return None
    return dict(item)


def mark_used(token_hash, table_ref=None):
    """Best-effort last-used stamp for the console; never blocks a request."""
    try:
        (table_ref or table()).update_item(
            Key={"token_hash": token_hash},
            UpdateExpression="SET last_used_at = :now",
            ExpressionAttributeValues={":now": _now()},
        )
    except Exception:
        pass


def revoke(token_id, table_ref=None):
    """Revoke one token by ID. Returns ``(status, payload)``."""
    token_id = str(token_id or "").strip()
    if not token_id:
        return 400, {"error": "Token ID is required"}
    table_ref = table_ref or table()
    for item in list_all(table_ref):
        if item.get("token_id") == token_id:
            if item.get("revoked_at"):
                return 200, public_view(item)
            updated = {**item, "revoked_at": _now()}
            table_ref.put_item(Item=updated)
            return 200, public_view(updated)
    return 404, {"error": f"No API token named {token_id}"}


def list_all(table_ref=None):
    items = (table_ref or table()).scan(Limit=200).get("Items", [])
    return sorted(items, key=lambda item: (item.get("created_at", ""), item.get("token_id", "")))


PUBLIC_FIELDS = (
    "token_id", "token_prefix", "agent", "subject",
    "created_by", "created_at", "last_used_at", "revoked_at",
)


def public_view(item):
    return {key: item.get(key) for key in PUBLIC_FIELDS}


def api_list(table_ref=None):
    return 200, {"tokens": [public_view(item) for item in list_all(table_ref)]}


def api_create(body, operator, table_ref=None):
    return create(body or {}, operator, table_ref=table_ref)


def api_revoke(token_id, table_ref=None):
    return revoke(token_id, table_ref=table_ref)
