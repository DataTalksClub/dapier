"""Device-pairing sessions for the dapier CLI (``dapier auth login``).

Cognito's hosted UI has no RFC 8628 device grant, so dapier brokers one:

1. ``POST /api/agent/device/start`` mints a one-time pairing: a secret
   ``device_code`` for the CLI and a short human ``user_code``.
2. The operator signs in through the console and approves the code at
   ``/device``; the approval carries the browser session's DTC identity.
3. The CLI polls ``POST /api/agent/device/token`` and receives a
   dapier-issued device session (``dapd_…``), which authenticates
   agent-API calls as the operator's stable DTC subject — the same
   identity a loopback DTC ID token carries, so grants, operator checks,
   and audit records are unchanged.

Only hashes are stored as keys. The plaintext session token rests on the
pending pairing just long enough for one poll (the pairing TTL bounds the
window), which is the unavoidable exposure of a brokered device flow.
Sessions expire after SESSION_TTL_DAYS; ``refresh`` rotates them and
``revoke`` kills them immediately.
"""

import base64
import hashlib
import os
import secrets
import time
from datetime import datetime, timezone

PREFIX = "dapd_"
USER_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # no 0/O/1/I/L
USER_CODE_LENGTH = 8
PAIRING_TTL_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 30 * 86400
POLL_INTERVAL_SECONDS = 2
# Grace window for the rotated-out token after a refresh, so a CLI that
# races itself (or two processes sharing a session file) is not locked out.
ROTATION_GRACE_SECONDS = 120


def table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["DEVICE_SESSIONS_TABLE"])


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _hash(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def _secret():
    return PREFIX + base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def normalize_user_code(raw):
    """Uppercase alphanumerics only: 'abcd efgh' and 'ABCD-EFGH' both match."""
    allowed = set(USER_CODE_ALPHABET)
    return "".join(char for char in str(raw or "").upper() if char in allowed)


def _expired(item, now=None):
    now = now if now is not None else time.time()
    return int(item.get("ttl", 0)) <= now


def start(table_ref=None):
    """Create one pending pairing. Returns ``(device_code, view)``."""
    store = table_ref or table()
    device_code = _secret()
    user_code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
    ttl = int(time.time()) + PAIRING_TTL_SECONDS
    item = {
        "pk": f"pairing#{_hash(device_code)}",
        "kind": "pairing",
        "user_code": user_code,
        "status": "pending",
        "created_at": _now_iso(),
        "ttl": ttl,
    }
    store.put_item(Item=item)
    store.put_item(Item={
        "pk": f"usercode#{user_code}",
        "kind": "usercode",
        "device_hash": item["pk"],
        "ttl": ttl,
    })
    return device_code, {
        "user_code": "-".join((user_code[:4], user_code[4:])),
        "expires_in": PAIRING_TTL_SECONDS,
        "interval": POLL_INTERVAL_SECONDS,
    }


def approve(user_code, subject, email, table_ref=None):
    """Pair an approved code with a DTC identity. Returns 'ok' or 'invalid'."""
    store = table_ref or table()
    code = normalize_user_code(user_code)
    if len(code) != USER_CODE_LENGTH or not subject:
        return "invalid"
    pointer = store.get_item(Key={"pk": f"usercode#{code}"}).get("Item")
    if not pointer or pointer.get("kind") != "usercode" or _expired(pointer):
        return "invalid"
    pairing = store.get_item(Key={"pk": pointer["device_hash"]}).get("Item")
    if not pairing or pairing.get("kind") != "pairing" or _expired(pairing):
        return "invalid"
    token, expires_at = _put_session(store, subject, email)
    try:
        store.update_item(
            Key={"pk": pairing["pk"]},
            UpdateExpression="SET #st = :approved, session = :session",
            ConditionExpression="attribute_exists(pk) AND #st = :pending",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":approved": "approved",
                ":pending": "pending",
                ":session": {
                    "token": token, "subject": str(subject), "email": str(email or ""),
                    "expires_at": expires_at,
                },
            },
        )
    except Exception:
        # Lost the approval race: drop the session we just minted so no
        # orphan credential outlives the pairing that lost it.
        store.delete_item(Key={"pk": f"session#{_hash(token)}"})
        return "invalid"
    store.delete_item(Key={"pk": f"usercode#{code}"})
    return "ok"


def poll(device_code, table_ref=None):
    """CLI poll. Returns pending / expired / approved exactly once."""
    store = table_ref or table()
    if not device_code or not str(device_code).startswith(PREFIX):
        return {"status": "expired"}
    pairing = store.get_item(Key={"pk": f"pairing#{_hash(device_code)}"}).get("Item")
    if not pairing or pairing.get("kind") != "pairing" or _expired(pairing):
        return {"status": "expired"}
    if pairing.get("status") != "approved":
        return {"status": "pending"}
    store.delete_item(Key={"pk": pairing["pk"]})
    session = pairing.get("session") or {}
    return {
        "status": "approved",
        "token": session.get("token", ""),
        "subject": session.get("subject", ""),
        "email": session.get("email", ""),
        "expires_at": int(session.get("expires_at", 0)),
    }


def _put_session(store, subject, email):
    token = _secret()
    now = int(time.time())
    store.put_item(Item={
        "pk": f"session#{_hash(token)}",
        "kind": "session",
        "subject": str(subject),
        "email": str(email or ""),
        "created_at": _now_iso(),
        "ttl": now + SESSION_TTL_SECONDS,
    })
    return token, now + SESSION_TTL_SECONDS


def create_session(subject, email, table_ref=None):
    """Mint a session directly (used by tests and future enrollment paths)."""
    return _put_session(table_ref or table(), subject, email)


def resolve(bearer, table_ref=None):
    """Session identity for a presented ``dapd_…`` bearer, or None."""
    if not bearer or not bearer.startswith(PREFIX):
        return None
    item = (table_ref or table()).get_item(
        Key={"pk": f"session#{_hash(bearer)}"}
    ).get("Item")
    if not item or item.get("kind") != "session" or _expired(item):
        return None
    return {"subject": item["subject"], "email": item.get("email", "")}


def refresh(bearer, table_ref=None):
    """Rotate one session. Returns ``(new_token, expires_at)`` or None.

    The old item is claimed (rotated_at stamped, TTL cut to the grace
    window) before the replacement is minted, so a second refresh with the
    same token — a replay or a racing process — fails instead of forking a
    second live chain.
    """
    store = table_ref or table()
    if not bearer or not bearer.startswith(PREFIX):
        return None
    key = {"pk": f"session#{_hash(bearer)}"}
    old = store.get_item(Key=key).get("Item")
    if not old or old.get("kind") != "session" or _expired(old):
        return None
    try:
        store.update_item(
            Key=key,
            UpdateExpression="SET ttl = :grace, rotated_at = :now",
            ConditionExpression="attribute_exists(pk) AND attribute_not_exists(rotated_at)",
            ExpressionAttributeValues={
                ":grace": int(time.time()) + ROTATION_GRACE_SECONDS,
                ":now": _now_iso(),
            },
        )
    except Exception:
        return None  # already rotated elsewhere
    return _put_session(store, old["subject"], old.get("email", ""))


def revoke(bearer, table_ref=None):
    """Delete one session. Returns True when a live session was removed."""
    if not bearer or not bearer.startswith(PREFIX):
        return False
    key = {"pk": f"session#{_hash(bearer)}"}
    store = table_ref or table()
    item = store.get_item(Key=key).get("Item")
    if not item or item.get("kind") != "session":
        return False
    store.delete_item(Key=key)
    return True
