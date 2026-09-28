"""Named OAuth connection records.

A connection binds one provider account to one ``connection_id``. Bearer
credentials always live in the credentials store; this module owns the
DynamoDB metadata item only:

- ``connection_id`` (HASH key), ``provider``, ``display_name``
- ``scopes`` (requested), ``granted_scopes``
- ``expected_account_id`` / ``verified_account_id`` / ``account_title``
- ``owner_subject`` (stable DTC subject that connected it)
- ``credential_id`` (pointer into the credentials store, never a secret)
- ``status``: ``ready`` (created, awaiting consent) or ``connected``
- ``version`` (bumped on every metadata edit), timestamps
"""

import base64
import json
import re
from datetime import datetime, timezone

from .providers import oauth_providers

CONNECTION_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{1,62}")

STATUS_READY = "ready"
STATUS_CONNECTED = "connected"
# Set by the token lifecycle (connections.tokens) after provider-side
# revocation; the connection must be reconnected before any use.
STATUS_REVOKED = "revoked"

# Providers that authenticate with a directly supplied token instead of an
# OAuth consent round-trip (see importing.save_token_connection). Zoom's meeting
# connections are regular OAuth; its webhook signing token is not a provider
# credential — the console's webhook setup has its own provider=="zoom" path.
TOKEN_PROVIDERS = {"slack", "telegram"}


class ConnectionError(ValueError):
    """The connection request or transition is invalid."""


class BindingError(ConnectionError):
    """The verified provider account does not match the bound account."""


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def validate_connection_id(connection_id):
    connection_id = str(connection_id or "").strip().lower()
    if not CONNECTION_ID_RE.fullmatch(connection_id):
        raise ConnectionError(
            "Connection ID must use lowercase letters, numbers, dashes, or underscores"
        )
    return connection_id


def validate_new_connection(body):
    """Validate a create/edit request body. Returns cleaned fields.

    Raises ConnectionError with a user-facing message. Never returns secrets.
    OAuth client credentials are not required: they deploy with the stack
    (see ``oauth_clients``). Explicit values are still accepted here — the
    CLI import path uses them for refresh tokens issued by another client.
    """
    body = body or {}
    connection_id = validate_connection_id(body.get("connection_id"))
    provider = str(body.get("provider", "")).strip().lower()
    if provider not in oauth_providers.PROVIDERS and provider not in TOKEN_PROVIDERS:
        raise ConnectionError(
            f"Provider must be one of: "
            f"{', '.join(sorted(set(oauth_providers.PROVIDERS) | TOKEN_PROVIDERS))}"
        )
    client_id = str(body.get("client_id", "")).strip() or None
    client_secret = str(body.get("client_secret", "")).strip() or None
    raw_scopes = body.get("scopes", [])
    if isinstance(raw_scopes, str):
        raw_scopes = raw_scopes.split()
    scopes = [scope for scope in raw_scopes if isinstance(scope, str) and scope.strip()]
    try:
        if provider in oauth_providers.PROVIDERS:
            scopes = oauth_providers.normalize_scopes(provider, scopes)
        else:
            # Token providers carry their scopes in the token itself.
            scopes = sorted({scope for scope in scopes if scope})
    except oauth_providers.ProviderError as exc:
        raise ConnectionError(str(exc))
    expected_account_id = str(body.get("expected_account_id", "") or "").strip() or None
    root_path = body.get("root_path")
    root_path = "" if root_path is None else str(root_path).strip()
    if root_path:
        if provider != "dropbox":
            raise ConnectionError("root_path applies only to Dropbox connections")
        root_path = root_path.strip("/")
        root_path = f"/{root_path}" if root_path else ""
    return {
        "connection_id": connection_id,
        "provider": provider,
        "display_name": str(body.get("display_name") or connection_id).strip()[:100],
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": scopes,
        "expected_account_id": expected_account_id,
        # None = the request didn't mention it (build_item keeps the previous
        # value); "" = explicitly clear (list the whole Dropbox).
        "root_path": root_path if "root_path" in body else None,
    }


def credential_id_for(connection_id):
    return f"oauth#{connection_id}"


def build_item(fields, *, owner_subject, previous=None):
    """Build the DynamoDB item for a create or edit.

    Edits preserve the verified provider binding and bump ``version``.
    Changing ``expected_account_id`` after first verified consent requires
    ``replace_binding=True`` in ``fields``.
    """
    now = now_iso()
    previous = previous or {}
    verified = previous.get("verified_account_id")
    if verified and fields.get("expected_account_id") != previous.get("expected_account_id"):
        if not fields.get("replace_binding"):
            raise BindingError(
                "This connection is already bound to a verified provider account; "
                "reconnect with an explicit binding replacement to change it"
            )
        verified = None
    return {
        "connection_id": fields["connection_id"],
        "provider": fields["provider"],
        "display_name": fields["display_name"],
        "scopes": fields["scopes"],
        "granted_scopes": list(previous.get("granted_scopes") or []),
        "expected_account_id": fields.get("expected_account_id"),
        # None = the request didn't mention it; "" = explicitly clear (list
        # the whole Dropbox).
        "root_path": (previous.get("root_path") or "") if fields.get("root_path") is None
        else fields["root_path"],
        "verified_account_id": verified,
        "account_title": previous.get("account_title"),
        "owner_subject": owner_subject or previous.get("owner_subject"),
        "credential_id": credential_id_for(fields["connection_id"]),
        "status": previous.get("status", STATUS_READY),
        "version": int(previous.get("version", 0)) + 1,
        "created_at": previous.get("created_at", now),
        "updated_at": now,
        **({"connected_at": previous["connected_at"]} if previous.get("connected_at") else {}),
        **({"connected_by": previous["connected_by"]} if previous.get("connected_by") else {}),
    }


def check_binding(item, verified_account_id):
    """Enforce the provider-account binding for ``item``.

    The account ID is immutable after first verified consent: binding a
    different account (e.g. the DataTalksClub channel onto ``youtube-personal``)
    raises BindingError before the connection is marked ready.
    """
    expected = item.get("expected_account_id")
    if expected and verified_account_id != expected:
        raise BindingError(
            f"Provider account {verified_account_id} does not match the expected "
            f"account for connection {item['connection_id']}"
        )
    bound = item.get("verified_account_id")
    if bound and verified_account_id != bound:
        raise BindingError(
            f"Connection {item['connection_id']} is already bound to a different "
            "provider account; reconnect with an explicit binding replacement"
        )
    return verified_account_id


def mark_connected(item, *, verified_account_id, account_title, granted_scopes, connected_by):
    """Return an updated item marked connected after verification."""
    check_binding(item, verified_account_id)
    now = now_iso()
    updated = dict(item)
    updated.update({
        "verified_account_id": verified_account_id,
        "account_title": account_title,
        "granted_scopes": granted_scopes,
        "status": STATUS_CONNECTED,
        "connected_at": now,
        "connected_by": connected_by,
        "updated_at": now,
        "version": int(item.get("version", 0)) + 1,
    })
    # A connection created without a name (the API default is the
    # connection_id) takes the verified account identity — bot handle,
    # workspace, account email — as its display name. That is what tells
    # several accounts of one provider apart; an operator's explicit rename
    # (anything other than the default) always wins.
    if account_title and updated.get("display_name") in (None, "", updated["connection_id"]):
        updated["display_name"] = account_title
    return updated


def token_expires_at(stored):
    """The stored token's expiry as an ISO instant, or None when it has none.

    ``stored`` is the connection's credential value (tokens live in the
    credentials store, not the metadata item); ``expires_at`` there is an
    epoch-seconds timestamp. Only the expiry field is read — no secret
    passes through.
    """
    try:
        epoch = int((stored or {}).get("expires_at"))
    except (TypeError, ValueError):
        return None
    if epoch <= 0:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def health(item, stored=None, *, now=None):
    """Computed connection health for listings: ``ok`` or ``expired``.

    ``expired`` means "needs reconnection": the stored access token is past
    its expiry (same skew the token refresh uses, so the two never
    disagree), or the connection was revoked and must be reconnected
    outright whatever the stored tokens say. Connections without expiring
    tokens (pasted bot tokens, awaiting consent) stay ``ok``. Callers that
    did not read the credential store pass ``stored=None`` and get the
    status-derived verdict only.
    """
    if item.get("status") == STATUS_REVOKED:
        return "expired"
    stored = stored or {}
    if "expires_at" not in stored:
        return "ok"
    return "expired" if oauth_providers.is_expired(stored, now=now) else "ok"


def public_view(item, stored=None):
    """Metadata safe for list/status responses (never secrets).

    ``stored`` is the connection's credential value when the caller has it
    (listing/show handlers read the store); the view then also carries
    ``token_expires_at`` (ISO, None without a stored expiry) and the
    computed ``health`` so surfaces can flag connections needing
    reconnection.
    """
    return {
        "connection_id": item.get("connection_id"),
        "provider": item.get("provider"),
        "display_name": item.get("display_name"),
        "scopes": item.get("scopes", []),
        "granted_scopes": item.get("granted_scopes", []),
        "expected_account_id": item.get("expected_account_id"),
        "root_path": item.get("root_path") or "",
        "verified_account_id": item.get("verified_account_id"),
        "account_title": item.get("account_title"),
        "status": item.get("status"),
        "health": health(item, stored),
        "token_expires_at": token_expires_at(stored),
        "version": item.get("version"),
        "updated_at": item.get("updated_at"),
        "connected_at": item.get("connected_at"),
    }


def get_connection(table, connection_id):
    return table.get_item(Key={"connection_id": connection_id}).get("Item")


def put_connection(table, item):
    table.put_item(Item=item)
    return item


def list_connections(table, limit=50):
    return table.scan(Limit=limit).get("Items", [])


# The paged list (api_list_connections). Connection metadata is small and
# nothing may silently clip at a scan page (connection #51 used to be
# unreachable through the overview's Limit=50 scan), so the list walks the
# whole table — bounded like every other walk — and windows the sorted
# result with an opaque token, the same contract as the runs list.
CONNECTIONS_DEFAULT_LIMIT = 50
CONNECTIONS_MAX_LIMIT = 200
CONNECTIONS_SCAN_PAGE = 300
CONNECTIONS_MAX_SCAN_PAGES = 100


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sort_key(item):
    """Connection-id order, so paging is stable across calls."""
    return str(item.get("connection_id") or "")


def _encode_token(item):
    """Opaque stateless page token: the last row's sort key."""
    raw = json.dumps({"c": _sort_key(item)}, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_token(token):
    """The connection id behind a page token, or None."""
    text = str(token or "")
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        return str(data.get("c") or "") or None
    except (ValueError, TypeError):
        return None


def _scan_all(table):
    """Every connection record, walking scan pages (bounded)."""
    items = []
    kwargs = {"Limit": CONNECTIONS_SCAN_PAGE}
    for _ in range(CONNECTIONS_MAX_SCAN_PAGES):
        page = table.scan(**kwargs)
        items.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return items


def api_list_connections(table, limit=None, next_token=None):
    """The paged connections list: ``(status, payload)``.

    Rows sort by connection id; ``paging.next`` carries the last returned
    row's sort key and the follow-up call passes it back as ``next_token``
    to start strictly after that row. An unparseable token is a 400. The
    shape is backward compatible — ``connections`` plus an additive
    ``paging`` block, the same contract as the runs list.
    """
    limit = max(1, min(_int(limit) or CONNECTIONS_DEFAULT_LIMIT, CONNECTIONS_MAX_LIMIT))
    token_key = _decode_token(next_token) if next_token else None
    if next_token and token_key is None:
        return 400, {"error": "Invalid page token"}
    items = sorted(_scan_all(table), key=_sort_key)
    if token_key is not None:
        items = [item for item in items if _sort_key(item) > token_key]
    page = items[:limit]
    more = len(items) > limit
    return 200, {
        "connections": page,
        "paging": {
            "next": _encode_token(page[-1]) if more and page else None,
            "limit": limit,
        },
    }
