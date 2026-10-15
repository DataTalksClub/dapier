"""Device-pairing grants for the dapier CLI (``dapier auth login``).

Cognito's hosted UI has no RFC 8628 device grant, so dapier brokers one,
shaped like the DataOps implementation this was ported from:

1. ``POST /api/agent/device/start`` mints a pairing: a secret
   ``device_code`` for the CLI and a short human ``user_code`` for the
   approval page, together with the machine label and requesting IP the
   operator reviews before deciding.
2. The operator signs in through the console, reads the pairing's facts
   (label, IP, age, time left) and approves or denies at ``/device``;
   the approval carries the browser session's DTC identity. Peeking and
   deciding share a five-attempt budget per pairing, so a guessed code
   runs out of tries long before the code space is explored.
3. The CLI polls ``POST /api/agent/device/token`` over the RFC 8628
   error vocabulary (``authorization_pending``, ``slow_down``,
   ``access_denied``, ``expired_token``) and receives, exactly once, a
   dapier-issued device session (``dapd_...``), which authenticates
   agent-API calls as the operator's stable DTC subject - the same
   identity a loopback DTC ID token carries, so grants, operator checks,
   and audit records are unchanged.

Nothing usable is ever persisted in plaintext: the pairing, user-code
and session rows are keyed by hashes, the pairing never carries the
user code, and the session bearer never touches the table at all. An
approval records only the decision and the approving identity; the
winning poll generates the bearer in memory and, in ONE DynamoDB
transaction, consumes the approved, unexpired grant and creates the
session row keyed by the bearer's hash. Only a committed transaction
returns the bearer, so exactly one poll ever receives it, and a losing,
stale or late poll writes nothing (revocations and rotations stand).
Sessions expire after SESSION_TTL_SECONDS; ``refresh`` rotates them and
``revoke`` kills them immediately.
"""

import base64
import hashlib
import os
import re
import secrets
import time
from datetime import datetime, timezone

PREFIX = "dapd_"
# No vowels and no confusable characters (no L/V, no 0/1): codes are read
# aloud and retyped by hand, and must never spell a word. This is dakit's
# canonical USER_CODE_ALPHABET (device-code.js), which the /device page
# validates against, so the server never issues a code the page rejects.
USER_CODE_ALPHABET = "BCDFGHJKMNPQRSTWXZ23456789"
USER_CODE_LENGTH = 8
PAIRING_TTL_SECONDS = 15 * 60
SESSION_TTL_SECONDS = 30 * 86400
POLL_INTERVAL_SECONDS = 5
# Shared peek+decision budget for one pairing, then the grant is destroyed.
DEVICE_MAX_ATTEMPTS = 5
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
    """Case and separator folding only - never truncation.

    'abcd efgh' and 'ABCD-EFGH' both match; a code with a wrong character
    keeps that character and simply never finds a pairing.
    """
    compact = re.sub(r"[^A-Z0-9]", "", str(raw or "").upper())
    if len(compact) != USER_CODE_LENGTH:
        return ""
    return "-".join((compact[:4], compact[4:]))


def sanitize_label(value):
    """A label the operator can recognise; the machine is untrusted."""
    label = str(value or "").strip()[:80]
    return label or "Dapier CLI"


def _expired(item, now=None):
    now = now if now is not None else time.time()
    return int(item.get("ttl", 0)) <= now


def start(table_ref=None, label="Dapier CLI", request_ip=""):
    """Create one pending pairing. Returns ``(device_code, view)``.

    The view carries no secret beyond the human code it shows; the
    verification URIs are attached by the route, which knows the
    request's host. Only the code hashes are persisted.
    """
    store = table_ref or table()
    device_code = _secret()
    user_code = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
    # The canonical form normalize_user_code returns, so every lookup
    # hashes the same string the code is stored and reviewed under.
    canonical = "-".join((user_code[:4], user_code[4:]))
    ttl = int(time.time()) + PAIRING_TTL_SECONDS
    item = {
        "pk": f"pairing#{_hash(device_code)}",
        "kind": "pairing",
        "user_code_hash": _hash(canonical),
        "status": "pending",
        "label": sanitize_label(label),
        "request_ip": str(request_ip or ""),
        "created_at": _now_iso(),
        "attempts": 0,
        "ttl": ttl,
    }
    store.put_item(Item=item)
    store.put_item(Item={
        "pk": f"usercode#{_hash(canonical)}",
        "kind": "usercode",
        "device_hash": item["pk"],
        "ttl": ttl,
    })
    return device_code, {
        "user_code": canonical,
        "expires_in": PAIRING_TTL_SECONDS,
        "interval": POLL_INTERVAL_SECONDS,
    }


def _pairing_for_code(store, code, now=None):
    """The pairing behind a normalized user code, or None."""
    pointer = store.get_item(Key={"pk": f"usercode#{_hash(code)}"}).get("Item")
    if not pointer or pointer.get("kind") != "usercode" or _expired(pointer, now):
        return None
    pairing = store.get_item(Key={"pk": pointer["device_hash"]}).get("Item")
    if not pairing or pairing.get("kind") != "pairing" or _expired(pairing, now):
        return None
    return pairing


def _conditional_failure(error):
    """True when DynamoDB refused a write on its condition (a plain
    conditional write, or a transaction cancelled by one)."""
    response = getattr(error, "response", None) or {}
    code = (response.get("Error") or {}).get("Code")
    if code == "TransactionCanceledException":
        return any((reason or {}).get("Code") == "ConditionalCheckFailed"
                   for reason in response.get("CancellationReasons") or [])
    return code == "ConditionalCheckFailedException" \
        or type(error).__name__ == "ConditionalCheckFailedException"


# The one guard every peek and decision passes, evaluated by DynamoDB in the
# same write that spends the attempt: still pending, not expired, and under
# the shared budget. The counter is incremented in place, never assigned from
# a value read earlier, so concurrent peeks and decisions can never spend more
# than DEVICE_MAX_ATTEMPTS between them.
_GUARD = ("attribute_exists(#pk) AND #st = :pending AND #ttl > :now "
          "AND (attribute_not_exists(#a) OR #a < :max)")
_GUARD_NAMES = {"#pk": "pk", "#st": "status", "#ttl": "ttl", "#a": "attempts"}


def _guard_values(now):
    return {":pending": "pending", ":now": int(now), ":max": DEVICE_MAX_ATTEMPTS,
            ":zero": 0, ":one": 1}


def _spend_or_destroy(store, pairing, error):
    """A failed guard. Only an exhausted grant that is *still pending* is
    destroyed, and that predicate is enforced by DynamoDB in the delete
    itself: a losing peek or decision never removes or alters a pairing a
    winner already approved or denied (its CLI still has to collect it).
    The request whose delete lands owns the pointer cleanup too."""
    if not _conditional_failure(error):
        raise error
    try:
        store.delete_item(
            Key={"pk": pairing["pk"]},
            ConditionExpression="attribute_exists(#pk) AND #st = :pending AND #a >= :max",
            ExpressionAttributeNames={"#pk": "pk", "#st": "status", "#a": "attempts"},
            ExpressionAttributeValues={":pending": "pending", ":max": DEVICE_MAX_ATTEMPTS},
        )
    except Exception as cleanup_error:
        if _conditional_failure(cleanup_error):
            return  # decided by someone else, or budget not spent: leave it
        raise
    user_code_hash = pairing.get("user_code_hash")
    if user_code_hash:
        store.delete_item(Key={"pk": f"usercode#{user_code_hash}"})


def lookup(user_code, table_ref=None, now=None):
    """The approval page's review facts for one pending pairing, or None.

    Every peek atomically spends one attempt from the pairing's shared
    budget; past it the pairing is destroyed and later peeks look like
    unknown codes.
    """
    code = normalize_user_code(user_code)
    if not code:
        return None
    store = table_ref or table()
    now = now if now is not None else time.time()
    pairing = _pairing_for_code(store, code, now)
    if not pairing or pairing.get("status") != "pending":
        return None
    try:
        store.update_item(
            Key={"pk": pairing["pk"]},
            UpdateExpression="SET #a = if_not_exists(#a, :zero) + :one",
            ConditionExpression=_GUARD,
            ExpressionAttributeNames=_GUARD_NAMES,
            ExpressionAttributeValues=_guard_values(now),
        )
    except Exception as error:
        _spend_or_destroy(store, pairing, error)
        return None
    return {
        "label": pairing.get("label", ""),
        "request_ip": pairing.get("request_ip", ""),
        "created_at": pairing.get("created_at", ""),
        "expires_at": int(pairing.get("ttl", 0)),
    }


def decide(user_code, approve, subject, email, table_ref=None, now=None):
    """Record the operator's decision on a pending pairing.

    The decision spends one attempt under the same atomic guard as a peek,
    so a decision racing a peek can never exceed the budget. Returns
    ``{"status": "approved"|"denied", "label": ...}`` or None when the code
    is unknown, expired, spent, or past its attempt budget.
    """
    code = normalize_user_code(user_code)
    if not code or not subject:
        return None
    store = table_ref or table()
    now = now if now is not None else time.time()
    pairing = _pairing_for_code(store, code, now)
    if not pairing or pairing.get("status") != "pending":
        return None
    names = dict(_GUARD_NAMES)
    values = _guard_values(now)
    if approve:
        # The approval records the decision and who made it — no bearer,
        # no token: the winning poll mints the session (see poll()).
        names["#sub"] = "subject"
        names["#em"] = "email"
        values[":approved"] = "approved"
        values[":subject"] = str(subject)
        values[":email"] = str(email or "")
        update = "SET #st = :approved, #sub = :subject, #em = :email, #a = if_not_exists(#a, :zero) + :one"
    else:
        names["#d"] = "decided_at"
        values[":denied"] = "denied"
        values[":decided"] = _now_iso()
        update = "SET #st = :denied, #d = :decided, #a = if_not_exists(#a, :zero) + :one"
    try:
        store.update_item(
            Key={"pk": pairing["pk"]},
            UpdateExpression=update,
            ConditionExpression=_GUARD,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception as error:
        try:
            _spend_or_destroy(store, pairing, error)
        except Exception:
            pass
        return None
    if approve:
        store.delete_item(Key={"pk": f"usercode#{_hash(code)}"})
    return {"status": "approved" if approve else "denied", "label": pairing.get("label", "")}


# Per-account budgets for the browser half, shared by every Lambda instance:
# a fixed window counter in this same table (expired by its ttl), so a cold
# start or a second concurrent instance never hands out a fresh budget.
PEEK_LIMIT = 20
DECISION_LIMIT = 10
LIMIT_WINDOW_SECONDS = 600


def allow(kind, subject, limit, *, window=LIMIT_WINDOW_SECONDS, table_ref=None, now=None):
    """Spend one unit of ``subject``'s ``kind`` budget for this window.

    Returns ``(True, 0)`` when allowed, else ``(False, retry_after_seconds)``.
    The subject is hashed into the key; the count is incremented atomically
    under a ``< limit`` condition, so concurrent callers cannot overspend.
    """
    store = table_ref or table()
    now = now if now is not None else time.time()
    bucket = int(now // window)
    window_end = (bucket + 1) * window
    try:
        store.update_item(
            Key={"pk": f"ratelimit#{kind}#{_hash(str(subject))}#{bucket}"},
            UpdateExpression="SET #c = if_not_exists(#c, :zero) + :one, #t = :expires, #k = :kind",
            ConditionExpression="attribute_not_exists(#c) OR #c < :limit",
            ExpressionAttributeNames={"#c": "count", "#t": "ttl", "#k": "kind"},
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":limit": int(limit),
                                       ":expires": window_end + window, ":kind": "ratelimit"},
        )
    except Exception as error:
        if not _conditional_failure(error):
            raise
        return False, max(1, int(window_end - now))
    return True, 0


def _grant_state(store, key, now):
    """The RFC 8628 answer for a grant as it stands now (no writes)."""
    current = store.get_item(Key=key).get("Item")
    if not current or current.get("kind") != "pairing" or _expired(current, now):
        return {"error": "expired_token"}
    if current.get("status") == "pending":
        return {"error": "authorization_pending"}
    if current.get("status") == "denied":
        return {"error": "access_denied"}
    return {"error": "expired_token"}


def poll(device_code, table_ref=None):
    """CLI poll over the RFC 8628 vocabulary.

    Returns ``{"error": ...}`` for pending/denied/expired outcomes, or the
    approved payload exactly once. Delivery is one transaction: delete the
    grant on condition it is still approved and unexpired, and put the new
    session row on condition its key is unused. The bearer exists only in
    this call's memory until that commit, and only a committed transaction
    returns it; any poll that loses (a concurrent or stale one, an expired
    grant at the final write) writes nothing and reports the grant's state.
    """
    store = table_ref or table()
    if not device_code or not str(device_code).startswith(PREFIX):
        return {"error": "expired_token"}
    key = {"pk": f"pairing#{_hash(device_code)}"}
    pairing = store.get_item(Key=key).get("Item")
    if not pairing or pairing.get("kind") != "pairing" or _expired(pairing, time.time()):
        if pairing:
            _delete_if(store, pairing, "#t <= :now", {"#t": "ttl"}, {":now": int(time.time())})
        return {"error": "expired_token"}
    status = pairing.get("status")
    if status == "pending":
        return {"error": "authorization_pending"}
    if status == "denied":
        # Report the denial once: whoever's conditional delete lands.
        if _delete_if(store, pairing, "#st = :denied", {"#st": "status"}, {":denied": "denied"}):
            return {"error": "access_denied"}
        return _grant_state(store, key, time.time())
    if status != "approved":
        return {"error": "expired_token"}

    token = _secret()  # in memory only until the transaction commits
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    now = int(time.time())
    try:
        store.meta.client.transact_write_items(TransactItems=[
            {"Delete": {
                "TableName": store.name,
                "Key": key,
                "ConditionExpression": "#st = :approved AND #t > :now",
                "ExpressionAttributeNames": {"#st": "status", "#t": "ttl"},
                "ExpressionAttributeValues": {":approved": "approved", ":now": now},
            }},
            {"Put": {
                "TableName": store.name,
                "Item": {
                    "pk": f"session#{_hash(token)}",
                    "kind": "session",
                    "subject": str(pairing.get("subject", "")),
                    "email": str(pairing.get("email", "")),
                    "created_at": _now_iso(),
                    "ttl": expires_at,
                },
                "ConditionExpression": "attribute_not_exists(#pk)",
                "ExpressionAttributeNames": {"#pk": "pk"},
            }},
        ])
    except Exception as error:
        if not _conditional_failure(error):
            raise
        return _grant_state(store, key, time.time())
    # Nothing may run between the commit and returning the bearer: the
    # grant is consumed and the session exists, so a failing extra write
    # here would strand an undeliverable session. (The approval already
    # removed the user-code pointer.)
    return {
        "token": token,
        "subject": pairing.get("subject", ""),
        "email": pairing.get("email", ""),
        "expires_at": expires_at,
    }


def _delete_if(store, pairing, condition, names, values):
    """Delete the grant (and its pointer) only if ``condition`` still holds."""
    try:
        store.delete_item(Key={"pk": pairing["pk"]}, ConditionExpression=condition,
                          ExpressionAttributeNames=names, ExpressionAttributeValues=values)
    except Exception as error:
        if _conditional_failure(error):
            return False
        raise
    user_code_hash = pairing.get("user_code_hash")
    if user_code_hash:
        store.delete_item(Key={"pk": f"usercode#{user_code_hash}"})
    return True


def _session_item(subject, email):
    token = _secret()
    item = {
        "pk": f"session#{_hash(token)}",
        "kind": "session",
        "subject": str(subject),
        "email": str(email or ""),
        "created_at": _now_iso(),
        "ttl": int(time.time()) + SESSION_TTL_SECONDS,
    }
    return token, item


def _put_session(store, subject, email):
    token, item = _session_item(subject, email)
    store.put_item(Item=item)
    return token, item["ttl"]


def create_session(subject, email, table_ref=None):
    """Mint a session directly (used by tests and future enrollment paths)."""
    return _put_session(table_ref or table(), subject, email)


def resolve(bearer, table_ref=None):
    """Session identity for a presented ``dapd_`` bearer, or None."""
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
    same token - a replay or a racing process - fails instead of forking a
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
            UpdateExpression="SET #t = :grace, #r = :now",
            ConditionExpression="attribute_exists(#pk) AND attribute_not_exists(#r)",
            ExpressionAttributeNames={"#pk": "pk", "#t": "ttl", "#r": "rotated_at"},
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
