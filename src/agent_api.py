"""DTC-authenticated agent API: connection status and short-lived tokens.

Callers present a DTC-issued ID token from the dedicated CLI client as
``Authorization: Bearer``. The API verifies issuer/audience/expiry/subject,
checks the caller's per-connection grant, and returns only an access token,
expiry, scope, and verified provider account ID over TLS with
``Cache-Control: no-store``. Refresh tokens and client secrets never leave
Dapier. Browser session cookies are not accepted here (no CSRF surface).
"""

import base64
import hashlib
import json
import os
import re
import time

from . import audit, authz, connections, oauth_providers, tokens
from .connections import BindingError
from .dtc_auth import verify_id_token
from .tokens import TokenError

RATE_WINDOW_SECONDS = 60

_BUCKETS = {}


def _json_response(status, body, *, headers=None):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", **(headers or {})},
        "body": json.dumps(body),
    }


def _no_store(body_status):
    body_status["headers"]["cache-control"] = "no-store"
    return body_status


def reset_rate_limits():
    _BUCKETS.clear()


def _rate_limit():
    try:
        return max(int(os.environ.get("TOKEN_RATE_LIMIT", "30")), 1)
    except ValueError:
        return 30


def _check_rate(subject, connection_id, *, now=None):
    now = now if now is not None else time.time()
    key = (subject, connection_id)
    window_start = now - RATE_WINDOW_SECONDS
    hits = [moment for moment in _BUCKETS.get(key, []) if moment > window_start]
    if len(hits) >= _rate_limit():
        _BUCKETS[key] = hits
        return False
    _BUCKETS[key] = hits + [now]
    return True


def _bearer(event):
    headers = event.get("headers") or {}
    value = next(
        (str(v) for k, v in headers.items() if k.lower() == "authorization"), "",
    )
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def authenticate(event):
    """Return ``(subject, None)`` or ``(None, error_response)``."""
    from .dtc_auth import auth_config

    cli_client_id = auth_config().get("cli_client_id", "")
    if not cli_client_id:
        return None, _json_response(503, {"error": "CLI token issuance is not configured"})
    token = _bearer(event)
    if not token:
        return None, _json_response(401, {"error": "DTC identity required"})
    try:
        claims = verify_id_token(token, audience=cli_client_id)
    except Exception:
        return None, _json_response(401, {"error": "Invalid DTC identity"})
    subject = claims.get("sub")
    if not subject:
        return None, _json_response(401, {"error": "Invalid DTC identity"})
    event["_dtc_claims"] = claims
    return str(subject), None


def _tables():
    import boto3

    resource = boto3.resource("dynamodb")
    return (
        resource.Table(os.environ["CONNECTIONS_TABLE"]),
        resource.Table(os.environ["GRANTS_TABLE"]),
    )


def issue_token(event):
    subject, error = authenticate(event)
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        return _json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid request"})
    connection_id = str(body.get("connection_id", "")).strip().lower()
    try:
        agent = authz.validate_agent(body.get("agent", ""))
    except ValueError as exc:
        return _json_response(400, {"error": str(exc)})
    if not connection_id:
        return _json_response(400, {"error": "Connection ID and agent are required"})
    if not _check_rate(subject, connection_id):
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="denied-rate-limited")
        return _json_response(429, {"error": "Token requests are rate-limited; retry shortly"})

    connections_table, grants_table = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    if not authz.check_grant(grants_table, subject=subject, agent=agent,
                             connection_id=connection_id, operation="use"):
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="denied-no-grant")
        return _json_response(403, {"error": "No grant for this connection and agent"})
    try:
        access_token, info = tokens.get_access_token(connection)
    except BindingError as exc:
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="error", error=str(exc))
        return _json_response(409, {"error": str(exc)})
    except TokenError as exc:
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="error", error=str(exc))
        return _json_response(502, {"error": "Provider token is unavailable"})
    audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="ok")
    return _no_store(_json_response(200, {
        "connection_id": connection_id,
        "provider": connection["provider"],
        "access_token": access_token,
        "expires_at": info["expires_at"],
        "scope": info["scope"],
        "provider_account_id": info["provider_account_id"],
        "account_title": info.get("account_title"),
        "refreshed": info["refreshed"],
    }))


def list_for_caller(event):
    subject, error = authenticate(event)
    if error:
        return error
    connections_table, grants_table = _tables()
    try:
        items = grants_table.scan(Limit=200).get("Items", [])
    except Exception:
        items = []
    mine = [item for item in items if item.get("subject") == subject and not _grant_expired(item)]
    views = []
    for item in mine:
        connection = connections.get_connection(connections_table, item["connection_id"])
        if not connection:
            continue
        views.append({
            **connections.public_view(connection),
            "agent": item.get("agent"),
            "operations": item.get("operations", []),
        })
    views.sort(key=lambda view: (view["connection_id"], view.get("agent") or ""))
    return _json_response(200, {"connections": views})


def _grant_expired(item):
    expires_at = item.get("expires_at")
    if not expires_at:
        return False
    try:
        return int(expires_at) <= int(time.time())
    except (TypeError, ValueError):
        return True


def show_connection(event, connection_id):
    subject, error = authenticate(event)
    if error:
        return error
    query = event.get("queryStringParameters") or {}
    agent = str(query.get("agent", "") or "").strip().lower() or None
    connections_table, grants_table = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    if agent:
        try:
            authz.validate_agent(agent)
        except ValueError as exc:
            return _json_response(400, {"error": str(exc)})
        pairs = [(subject, agent)]
    else:
        try:
            items = grants_table.scan(Limit=200).get("Items", [])
        except Exception:
            items = []
        pairs = [
            (item.get("subject"), item.get("agent"))
            for item in items
            if item.get("connection_id") == connection_id and not _grant_expired(item)
        ]
    allowed = any(
        authz.check_grant(grants_table, subject=pair_subject, agent=pair_agent,
                          connection_id=connection_id, operation="use")
        for pair_subject, pair_agent in pairs
        if pair_subject == subject
    )
    if not allowed:
        return _json_response(404, {"error": "Connection not found"})
    return _json_response(200, connections.public_view(connection))


def route(event, method, path):
    if method == "GET" and path == "/api/agent/config":
        return public_config()
    if method == "POST" and path == "/api/agent/token":
        return issue_token(event)
    if method == "GET" and path == "/api/agent/connections":
        return list_for_caller(event)
    match = re.fullmatch(r"/api/agent/connections/([a-z0-9_-]+)", path)
    if match and method == "GET":
        return show_connection(event, match.group(1))
    connect_match = re.fullmatch(r"/api/agent/connections/([a-z0-9_-]+)/connect", path)
    if connect_match and method == "POST":
        return start_connect(event, connect_match.group(1))
    if method == "POST" and path == "/api/agent/connections/import":
        return import_connection(event)
    return _json_response(404, {"error": "Not found"})


def public_config():
    from .dtc_auth import auth_config

    config = auth_config()
    return _json_response(200, {
        "auth_base_url": config["base_url"],
        "cli_client_id": config["cli_client_id"],
        "issuer": config["issuer"],
        "jwks_url": config["jwks_url"],
    })


def _b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def start_connect(event, connection_id):
    """Initiate a CLI-bound provider consent. Returns the authorize URL.

    Allowed for operators (by stable subject or email claim) or callers with
    a ``connect`` grant for the requested agent. The signed state binds the
    operator subject and PKCE verifier, so the public callback can complete
    the flow without a browser session cookie.
    """
    subject, error = authenticate(event)
    if error:
        return error
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        return _json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid request"})
    try:
        agent = authz.validate_agent(body.get("agent", ""))
    except ValueError as exc:
        return _json_response(400, {"error": str(exc)})

    from . import admin as admin_module

    connections_table, grants_table = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    claims = event.get("_dtc_claims") or {}
    email = claims.get("email", "")
    allowed = (
        subject in _operator_subjects()
        or (email and email.lower() in _operator_emails())
        or authz.check_grant(grants_table, subject=subject, agent=agent,
                             connection_id=connection_id, operation="connect")
    )
    if not allowed:
        audit.emit(connection_id, audit.CALLBACK, subject, agent=agent,
                   outcome="denied-not-authorized")
        return _json_response(403, {"error": "Not authorized to connect this connection"})
    redirect_uri = admin_module.oauth_callback_url()
    if not redirect_uri:
        return _json_response(503, {"error": "OAuth callback URL is not configured"})
    try:
        scopes = oauth_providers.normalize_scopes(connection["provider"], connection.get("scopes"))
    except oauth_providers.ProviderError as exc:
        return _json_response(400, {"error": str(exc)})
    verifier = _b64encode(os.urandom(48))
    challenge = _b64encode(hashlib.sha256(verifier.encode()).digest())
    state = admin_module._sign({
        "kind": "oauth",
        "connection_id": connection_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "jti": _b64encode(os.urandom(16)),
        "operator_subject": subject,
        "exp": int(time.time()) + 600,
    })
    audit.emit(connection_id, audit.CALLBACK, subject, agent=agent, outcome="connect-started")
    return _json_response(200, {
        "connection_id": connection_id,
        "authorize_url": oauth_providers.authorization_url(
            connection["provider"],
            client_id=connection["client_id"],
            redirect_uri=redirect_uri,
            scopes=scopes,
            state=state,
            code_challenge=challenge,
        ),
        "expires_in": 600,
    })


def _operator_subjects():
    return {part.strip() for part in os.environ.get("OPERATOR_SUBJECTS", "").split(",") if part.strip()}


def _operator_emails():
    return {part.strip().lower() for part in os.environ.get("OPERATOR_EMAILS", "").split(",") if part.strip()}


def import_core(body, *, operator_subject, connections_table):
    """Shared operator import. Returns ``(status_code, payload)``.

    Transfers the supplied refresh credential without logging it, verifies
    refresh + provider account before storing, and binds the connection.
    Existing backups are never touched.
    """
    from .credentials import put_credential

    if not isinstance(body.get("authorized_user"), dict):
        return 400, {"error": "An authorized-user credential object is required"}
    try:
        fields = connections.validate_new_connection(body)
    except connections.ConnectionError as exc:
        return 400, {"error": str(exc)}
    refresh_token = body["authorized_user"].get("refresh_token")
    if not refresh_token:
        return 400, {"error": "The authorized-user object has no refresh token"}

    try:
        token_data = oauth_providers.refresh_access_token(
            fields["provider"],
            refresh_token=refresh_token,
            client_id=fields["client_id"],
            client_secret=fields["client_secret"],
        )
    except oauth_providers.ProviderError as exc:
        audit.emit(fields["connection_id"], audit.IMPORT, operator_subject,
                   outcome="error", error=str(exc))
        return 400, {"error": f"Refresh check failed: {exc}"}
    try:
        account_id, account_title = oauth_providers.verify_account(
            fields["provider"], token_data["access_token"],
        )
    except oauth_providers.ProviderError as exc:
        audit.emit(fields["connection_id"], audit.IMPORT, operator_subject,
                   outcome="error", error=str(exc))
        return 400, {"error": f"Could not verify the provider account: {exc}"}

    previous = connections.get_connection(connections_table, fields["connection_id"])
    try:
        item = connections.build_item(fields, owner_subject=operator_subject, previous=previous)
        connections.check_binding(item, account_id)
        item = connections.mark_connected(
            item, verified_account_id=account_id, account_title=account_title,
            granted_scopes=fields["scopes"], connected_by=operator_subject,
        )
    except connections.ConnectionError as exc:
        audit.emit(fields["connection_id"], audit.IMPORT, operator_subject,
                   outcome="denied-account-mismatch", error=str(exc))
        status = 409 if isinstance(exc, connections.BindingError) else 400
        return status, {"error": str(exc)}
    stored = {
        "client_secret": fields["client_secret"],
        **oauth_providers.normalize_token_data(
            token_data, previous_refresh_token=refresh_token,
        ),
    }
    put_credential(item["credential_id"], stored, provider=item["provider"])
    connections.put_connection(connections_table, item)
    audit.emit(item["connection_id"], audit.IMPORT, operator_subject, outcome="ok")
    return 200, connections.public_view(item)


def import_connection(event):
    subject, error = authenticate(event)
    if error:
        return error
    claims = event.get("_dtc_claims") or {}
    email = str(claims.get("email", "")).lower()
    if subject not in _operator_subjects() and email not in _operator_emails():
        audit.emit("unknown", audit.IMPORT, subject, outcome="denied-not-operator")
        return _json_response(403, {"error": "Operator authorization required"})
    try:
        body = json.loads(event.get("body") or "{}")
    except (ValueError, AttributeError):
        return _json_response(400, {"error": "Invalid request"})
    if not isinstance(body, dict):
        return _json_response(400, {"error": "Invalid request"})
    connections_table, _ = _tables()
    status, payload = import_core(body, operator_subject=subject, connections_table=connections_table)
    return _json_response(status, payload)
