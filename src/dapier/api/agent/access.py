"""Access-management endpoints: connection grants, API tokens,
shared OAuth clients, and provider credentials."""

import json

from ...auth import api_tokens, authz
from ...connections import credentials
from ...connections.providers import oauth_clients

from .common import _json_response

from .common import _LateBinding

# Shared dependencies resolved through the agent package at call time:
# tests patch agent.<name> and every route module must see the patch.
authenticate = _LateBinding("authenticate")
require_operator = _LateBinding("require_operator")
_is_operator = _LateBinding("_is_operator")
_tables = _LateBinding("_tables")
audit = _LateBinding("audit")
verify_id_token = _LateBinding("verify_id_token")


__all__ = ["credentials_api", "grants_api", "oauth_clients_api", "oauth_clients_view", "tokens_api"]



def grants_api(event, method):
    """Operator-only grant management over the CLI's bearer authentication.

    Mirrors the console's /api/admin/grants endpoints on top of the shared
    grant logic in authz.
    """
    subject, error = require_operator(event, audit.GRANT)
    if error:
        return error
    if method == "GET":
        query = event.get("queryStringParameters") or {}
        status, payload = authz.api_list_grants(
            authz.grants_table(), connection_id=query.get("connection_id") or None,
            limit=query.get("limit"), next_token=query.get("next") or None,
        )
        return _json_response(status, payload)
    if method == "PUT":
        try:
            body = json.loads(event.get("body") or "{}")
        except (ValueError, AttributeError, json.JSONDecodeError):
            return _json_response(400, {"error": "Invalid request"})
        if not isinstance(body, dict):
            return _json_response(400, {"error": "Invalid request"})
        connections_table, _ = _tables()
        status, payload = authz.api_save_grant(
            authz.grants_table(), body, operator=subject,
            connections_table=connections_table,
        )
        if status == 200:
            audit.emit(payload["connection_id"], audit.GRANT, subject,
                       agent=payload["agent"], outcome="ok")
        return _json_response(status, payload)
    query = event.get("queryStringParameters") or {}
    status, payload = authz.api_delete_grant(
        authz.grants_table(), query.get("connection_id"), query.get("grantee"),
    )
    if status == 200:
        audit.emit(str(query.get("connection_id", "")).strip().lower(),
                   audit.GRANT, subject, outcome="revoked")
    return _json_response(status, payload)


def tokens_api(event, method):
    """Operator-only API-token management over the CLI's bearer authentication.

    Mirrors the console's /api/admin/tokens endpoints on top of the shared
    token logic in api_tokens. The create response carries the plaintext
    token exactly once; DELETE revokes, and DELETE with purge=1 permanently
    removes an already-revoked token together with its grants.
    """
    subject, error = require_operator(event, audit.API_TOKEN)
    if error:
        return error
    if method == "GET":
        status, payload = api_tokens.api_list()
        return _json_response(status, payload)
    if method == "PUT":
        try:
            body = json.loads(event.get("body") or "{}")
        except (ValueError, AttributeError, json.JSONDecodeError):
            return _json_response(400, {"error": "Invalid request"})
        if not isinstance(body, dict):
            return _json_response(400, {"error": "Invalid request"})
        status, payload = api_tokens.api_create(body, operator=subject)
        if status == 200:
            audit.emit(f"api-token#{payload['token_id']}", audit.API_TOKEN, subject,
                       agent=payload["agent"], outcome="created")
        return _json_response(status, payload)
    query = event.get("queryStringParameters") or {}
    if query.get("purge") in ("1", "true", "yes"):
        status, payload = api_tokens.api_delete(
            query.get("token_id"), grants_table_ref=authz.grants_table())
        if status == 200:
            audit.emit(f"api-token#{payload['token_id']}", audit.API_TOKEN, subject,
                       outcome="deleted")
        return _json_response(status, payload)
    status, payload = api_tokens.api_revoke(query.get("token_id"))
    if status == 200:
        audit.emit(f"api-token#{payload.get('token_id', 'unknown')}", audit.API_TOKEN,
                   subject, outcome="revoked")
    return _json_response(status, payload)


def oauth_clients_view(event):
    """Operator-only view of the shared OAuth clients (no secrets)."""
    _, error = require_operator(event, audit.CONFIG)
    if error:
        return error
    return _json_response(200, {
        "clients": [oauth_clients.status(p) for p in oauth_clients.CANONICAL_PROVIDERS],
    })


def oauth_clients_api(event, provider):
    """Operator-only OAuth client storage over the CLI's bearer authentication."""
    subject, error = require_operator(event, audit.CONFIG)
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid request"})
    status, payload = oauth_clients.api_save_client(
        provider, body.get("client_id"), body.get("client_secret"))
    if status == 200:
        audit.emit(f"oauth-client#{payload['provider']}", audit.CONFIG, subject, outcome="ok")
    return _json_response(status, payload)


def credentials_api(event, provider):
    """Operator-only credential storage over the CLI's bearer authentication."""
    subject, error = require_operator(event, "credential")
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid request"})
    status, payload = credentials.api_save_credential(provider, body)
    audit.emit(provider, "credential", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)
