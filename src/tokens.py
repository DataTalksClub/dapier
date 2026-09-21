"""Provider access-token lifecycle for named connections.

- Returns a usable short-lived access token, refreshing when expired.
- Preserves a valid refresh token when rotation responses omit it.
- Verifies the provider account on every issuance and enforces the
  connection's binding: a different account never yields a token.
- Concurrent refreshes cannot regress the stored secret: writes are
  version-conditional with one retry after re-reading.
- Revocation clears tokens provider-side (best effort) and locally, so
  subsequent requests fail.
"""

from . import connections, oauth_providers
from .connections import BindingError
from .credentials import (
    VersionConflict,
    get_credential_record,
    put_credential_if_version,
)

STATUS_REVOKED = "revoked"


class TokenError(Exception):
    """No usable access token is available for the connection."""


def _record(connection_id):
    return get_credential_record(f"oauth#{connection_id}")


def _stored_tokens(record):
    value = record.get("value") or {}
    if not isinstance(value, dict):
        return {}
    return value


def refresh_and_store(connection, record, *, transport=None):
    """Refresh, verify, bind-check, then store. Returns ``(stored, refreshed)``.

    Raises TokenError (transient/verify problems, nothing stored) or
    BindingError (account mismatch, nothing stored, caller must audit loudly).
    """
    stored = _stored_tokens(record)
    refresh_token = stored.get("refresh_token")
    if not refresh_token:
        raise TokenError(f"Connection {connection['connection_id']} has no refresh token")
    try:
        response = oauth_providers.refresh_access_token(
            connection["provider"],
            refresh_token=refresh_token,
            client_id=connection["client_id"],
            client_secret=stored.get("client_secret") or "",
            transport=transport,
        )
    except oauth_providers.ProviderError as exc:
        raise TokenError(str(exc))
    normalized = oauth_providers.normalize_token_data(
        response, previous_refresh_token=refresh_token,
    )
    try:
        account_id, account_title = oauth_providers.verify_account(
            connection["provider"], normalized["access_token"], transport=transport,
        )
    except oauth_providers.ProviderError as exc:
        raise TokenError(str(exc))
    connections.check_binding(connection, account_id)
    new_value = {
        "client_secret": stored.get("client_secret"),
        **normalized,
    }
    expected = record.get("version", 0)
    try:
        put_credential_if_version(
            f"oauth#{connection['connection_id']}", new_value,
            provider=connection["provider"], expected_version=expected,
        )
    except VersionConflict:
        raise
    return new_value, True, account_id, account_title


def get_access_token(connection, *, transport=None, _retry=True):
    """Return ``(access_token, info)`` with a verified provider identity.

    ``info`` carries ``expires_at``, ``scope``, ``provider_account_id``,
    ``account_title``, and ``refreshed``. Raises TokenError or BindingError.
    """
    if connection.get("status") == STATUS_REVOKED:
        raise TokenError(f"Connection {connection['connection_id']} is revoked")
    record = _record(connection["connection_id"])
    stored = _stored_tokens(record)
    access_token = stored.get("access_token")
    if access_token and not oauth_providers.is_expired(stored):
        try:
            account_id, account_title = oauth_providers.verify_account(
                connection["provider"], access_token, transport=transport,
            )
        except oauth_providers.ProviderError as exc:
            raise TokenError(str(exc))
        connections.check_binding(connection, account_id)
        return access_token, {
            "expires_at": stored.get("expires_at"),
            "scope": stored.get("scope", ""),
            "provider_account_id": account_id,
            "account_title": account_title,
            "refreshed": False,
        }
    try:
        new_value, _, account_id, account_title = refresh_and_store(
            connection, record, transport=transport,
        )
    except VersionConflict:
        if not _retry:
            raise TokenError("Concurrent refresh conflict; retry the request")
        fresh = _record(connection["connection_id"])
        fresh_stored = _stored_tokens(fresh)
        if fresh_stored.get("access_token") and not oauth_providers.is_expired(fresh_stored):
            return get_access_token(connection, transport=transport, _retry=False)
        try:
            new_value, _, account_id, account_title = refresh_and_store(
                connection, fresh, transport=transport,
            )
        except VersionConflict:
            raise TokenError("Concurrent refresh conflict; retry the request")
    return new_value["access_token"], {
        "expires_at": new_value.get("expires_at"),
        "scope": new_value.get("scope", ""),
        "provider_account_id": account_id,
        "account_title": account_title,
        "refreshed": True,
    }


def revoke_connection(connection, *, transport=None):
    """Revoke tokens and mark the connection revoked. Returns the updated item."""
    record = _record(connection["connection_id"])
    stored = _stored_tokens(record)
    for token in (stored.get("access_token"), stored.get("refresh_token")):
        if token:
            oauth_providers.revoke_token(connection["provider"], token, transport=transport)
    cleared = {"client_secret": stored.get("client_secret")}
    try:
        put_credential_if_version(
            f"oauth#{connection['connection_id']}", cleared,
            provider=connection["provider"], expected_version=record.get("version", 0),
        )
    except VersionConflict:
        fresh = _record(connection["connection_id"])
        fresh_stored = _stored_tokens(fresh)
        put_credential_if_version(
            f"oauth#{connection['connection_id']}",
            {"client_secret": fresh_stored.get("client_secret")},
            provider=connection["provider"], expected_version=fresh.get("version", 0),
        )
    updated = dict(connection)
    updated["status"] = STATUS_REVOKED
    return updated
