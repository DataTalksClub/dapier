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

from . import api_tokens, audit, authz, connections, credentials, designer_store, email_triggers, hook_triggers, oauth_clients, oauth_providers, tokens
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
    """Return ``(subject, None)`` or ``(None, error_response)``.

    Two bearer kinds: a Dapier-issued API token (``dap_…``) authenticates
    as its machine subject, and any other value is verified as a DTC ID
    token. API tokens work even where CLI token issuance is unconfigured.
    """
    token = _bearer(event)
    if not token:
        return None, _json_response(401, {"error": "DTC identity required"})
    if token.startswith(api_tokens.PREFIX):
        item = api_tokens.verify(token)
        if not item:
            return None, _json_response(401, {"error": "Invalid API token"})
        event["_api_token"] = item
        api_tokens.mark_used(item["token_hash"])
        return item["subject"], None

    from .dtc_auth import auth_config

    cli_client_id = auth_config().get("cli_client_id", "")
    if not cli_client_id:
        return None, _json_response(503, {"error": "CLI token issuance is not configured"})
    try:
        claims = verify_id_token(token, audience=cli_client_id)
    except Exception:
        return None, _json_response(401, {"error": "Invalid DTC identity"})
    subject = claims.get("sub")
    if not subject:
        return None, _json_response(401, {"error": "Invalid DTC identity"})
    event["_dtc_claims"] = claims
    return str(subject), None


def _is_operator(event, subject):
    """Operator check that API tokens can never pass.

    An empty operator allowlist means every DTC-authenticated account may
    administer the console; a machine token must not inherit that, so the
    check is DTC-claims-only by construction.
    """
    if event.get("_api_token"):
        return False
    claims = event.get("_dtc_claims") or {}
    return authz.is_operator({"subject": subject, "sub": claims.get("email", "")})


def _api_token(event):
    return event.get("_api_token")


def _check_agent_binding(event, agent):
    """Refuse API-token callers acting as any agent other than their own."""
    item = _api_token(event)
    return bool(item) and agent != item.get("agent")


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
    if _check_agent_binding(event, agent):
        bound = _api_token(event).get("agent")
        audit.emit(connection_id, audit.TOKEN, subject, agent=agent, outcome="denied-agent-mismatch")
        return _json_response(403, {"error": f"This API token is bound to agent '{bound}'"})
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
    if path == "/api/agent/email-triggers" and method in ("GET", "PUT", "DELETE"):
        return email_triggers_api(event, method)
    if path == "/api/agent/hook-triggers" and method in ("GET", "PUT", "DELETE"):
        return hook_triggers_api(event, method)
    if path == "/api/agent/grants" and method in ("GET", "PUT", "DELETE"):
        return grants_api(event, method)
    if path == "/api/agent/tokens" and method in ("GET", "PUT", "DELETE"):
        return tokens_api(event, method)
    if path == "/api/agent/overview" and method == "GET":
        return operator_overview(event)
    if path == "/api/agent/oauth-clients" and method == "GET":
        return oauth_clients_view(event)
    oauth_client_match = re.fullmatch(r"/api/agent/oauth-clients/([a-z]+)", path)
    if oauth_client_match and method == "PUT":
        return oauth_clients_api(event, oauth_client_match.group(1))
    credential_match = re.fullmatch(r"/api/agent/credentials/([a-z]+)", path)
    if credential_match and method == "PUT":
        return credentials_api(event, credential_match.group(1))
    revoke_match = re.fullmatch(r"/api/agent/connections/([a-z0-9_-]+)/tokens", path)
    if revoke_match and method == "DELETE":
        return revoke_connection_tokens(event, revoke_match.group(1))
    if path == "/api/agent/designer/workflows" and method in ("GET", "PUT"):
        return designer_api(event, method)
    designer_match = re.fullmatch(r"/api/agent/designer/workflows/([a-z0-9][a-z0-9._-]*\.yaml)", path)
    if designer_match and method == "GET":
        return designer_api(event, method, source=designer_match.group(1))
    return _json_response(404, {"error": "Not found"})


def require_operator(event, action):
    """Authenticate the bearer identity and require the operator allowlist.

    Returns ``(subject, None)`` or ``(None, error_response)``. ``action`` is
    the audit action recorded on a denial. API tokens never qualify.
    """
    subject, error = authenticate(event)
    if error:
        return None, error
    if not _is_operator(event, subject):
        audit.emit("unknown", action, subject, outcome="denied-not-operator")
        return None, _json_response(403, {"error": "Operator authorization required"})
    return subject, None


def designer_api(event, method, source=None):
    """Operator-only workflow designer API over the CLI's bearer authentication."""
    subject, error = require_operator(event, "workflow.save")
    if error:
        return error
    if method == "GET":
        status, payload = designer_store.api_get(source) if source else designer_store.api_list()
        return _json_response(status, payload)
    try:
        body = json.loads(event.get("body") or "{}")
        status, payload = designer_store.api_save(body)
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(str(payload.get("file", "unknown")), "workflow.save", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


def email_triggers_api(event, method):
    """Operator-only trigger management over the CLI's bearer authentication."""
    subject, error = require_operator(event, "email-trigger")
    if error:
        return error
    table_ref = email_triggers.get_table()
    try:
        if method == "GET":
            status, payload = email_triggers.api_list(table_ref)
        elif method == "PUT":
            body = json.loads(event.get("body") or "{}")
            status, payload = email_triggers.api_save(body, subject, table_ref=table_ref)
        else:
            query = event.get("queryStringParameters") or {}
            status, payload = email_triggers.api_delete(
                query.get("name", ""), subject, table_ref=table_ref,
            )
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(payload.get("name", "unknown"), "email-trigger", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


def hook_triggers_api(event, method):
    """Operator-only webhook/Telegram trigger management over the CLI's bearer authentication."""
    subject, error = require_operator(event, "hook-trigger")
    if error:
        return error
    table_ref = hook_triggers.get_table()
    try:
        if method == "GET":
            query = event.get("queryStringParameters") or {}
            kind = str(query.get("kind", "") or "").strip().lower() or None
            if kind and kind not in hook_triggers.KINDS:
                raise ValueError(f"hook kind must be one of: {', '.join(hook_triggers.KINDS)}")
            status, payload = hook_triggers.api_list(table_ref, kind=kind)
        elif method == "PUT":
            body = json.loads(event.get("body") or "{}")
            kind = str((body or {}).get("kind") or "webhook").strip().lower()
            if kind not in hook_triggers.KINDS:
                raise ValueError(f"hook kind must be one of: {', '.join(hook_triggers.KINDS)}")
            status, payload = hook_triggers.api_save(
                body, subject, kind, table_ref=table_ref,
                connections_table=_tables()[0],
            )
        else:
            query = event.get("queryStringParameters") or {}
            kind = str(query.get("kind", "") or "").strip().lower() or None
            status, payload = hook_triggers.api_delete(
                query.get("name", ""), subject, kind=kind, table_ref=table_ref,
                connections_table=_tables()[0],
            )
    except (ValueError, json.JSONDecodeError) as exc:
        return _json_response(400, {"error": str(exc) or "Invalid request"})
    audit.emit(payload.get("hook_id", "unknown"), "hook-trigger", subject,
               outcome="ok" if status == 200 else "error")
    return _json_response(status, payload)


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
    token exactly once; revocation is the only later change.
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
    status, payload = api_tokens.api_revoke(query.get("token_id"))
    if status == 200:
        audit.emit(f"api-token#{payload.get('token_id', 'unknown')}", audit.API_TOKEN,
                   subject, outcome="revoked")
    return _json_response(status, payload)


def operator_overview(event):
    """Operator-only read view mirroring the console overview."""
    _, error = require_operator(event, "overview")
    if error:
        return error
    from . import admin as admin_module

    return admin_module.overview()


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


def revoke_connection_tokens(event, connection_id):
    """Operator-only token revoke over the CLI's bearer authentication."""
    subject, error = require_operator(event, audit.REVOKE)
    if error:
        return error
    connections_table, _ = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    updated = tokens.revoke_connection(connection)
    connections.put_connection(connections_table, updated)
    audit.emit(connection_id, audit.REVOKE, subject, outcome="ok")
    return _json_response(200, {"connection_id": connection_id, "status": updated["status"]})


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
    if _check_agent_binding(event, agent):
        bound = _api_token(event).get("agent")
        audit.emit(connection_id, audit.CALLBACK, subject, agent=agent,
                   outcome="denied-agent-mismatch")
        return _json_response(403, {"error": f"This API token is bound to agent '{bound}'"})

    from . import admin as admin_module

    connections_table, grants_table = _tables()
    connection = connections.get_connection(connections_table, connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    allowed = (
        _is_operator(event, subject)
        or authz.check_grant(grants_table, subject=subject, agent=agent,
                             connection_id=connection_id, operation="connect")
    )
    if not allowed:
        audit.emit(connection_id, audit.CALLBACK, subject, agent=agent,
                   outcome="denied-not-authorized")
        return _json_response(403, {"error": "Not authorized to connect this connection"})
    redirect_uri = admin_module.oauth_callback_url()
    if connection["provider"] in connections.TOKEN_PROVIDERS:
        return _json_response(
            400,
            {"error": "This provider connects with a directly provided token; "
                      "an operator can paste a new one in the operator console"},
        )
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
    try:
        client_id, _ = oauth_clients.get(connection["provider"])
    except oauth_clients.ClientConfigError as exc:
        return _json_response(503, {"error": str(exc)})
    audit.emit(connection_id, audit.CALLBACK, subject, agent=agent, outcome="connect-started")
    return _json_response(200, {
        "connection_id": connection_id,
        "authorize_url": oauth_providers.authorization_url(
            connection["provider"],
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            state=state,
            code_challenge=challenge,
        ),
        "expires_in": 600,
    })


def _import_token_connection(body, *, operator_subject, connections_table):
    """Operator import for pasted-token providers (Slack, Telegram).

    Mirrors the console's token-connection path over the CLI's bearer
    authentication: the token is verified against its provider before it is
    stored, so an imported connection always carries a checked identity.
    """
    from . import slack_tokens, telegram_api
    from .credentials import put_credential

    try:
        fields = connections.validate_new_connection(body)
    except connections.ConnectionError as exc:
        return 400, {"error": str(exc)}
    token = str(body.get("token") or "").strip()
    if not token:
        return 400, {"error": "This provider imports with a token, not an authorized-user file"}
    from . import admin as admin_module

    try:
        account_id, account_title = admin_module._verify_token_provider(fields["provider"], token)
    except (slack_tokens.SlackTokenError, telegram_api.TelegramApiError) as exc:
        audit.emit(fields["connection_id"], audit.IMPORT, operator_subject,
                   outcome="error", error=str(exc))
        return 400, {"error": str(exc)}
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
    put_credential(item["credential_id"], {"token": token}, provider=item["provider"])
    connections.put_connection(connections_table, item)
    audit.emit(item["connection_id"], audit.IMPORT, operator_subject, outcome="ok")
    return 200, connections.public_view(item)


def import_core(body, *, operator_subject, connections_table):
    """Shared operator import. Returns ``(status_code, payload)``.

    Token providers (Slack, Telegram) import a verified pasted token; OAuth
    providers transfer the supplied refresh credential without logging it,
    verify refresh + provider account before storing, and bind the
    connection. Existing backups are never touched.
    """
    from .credentials import put_credential

    if str(body.get("provider", "")).strip().lower() in connections.TOKEN_PROVIDERS:
        return _import_token_connection(
            body, operator_subject=operator_subject, connections_table=connections_table)
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
        client_id, client_secret = oauth_clients.get(
            fields["provider"],
            client_id=fields.get("client_id"),
            client_secret=fields.get("client_secret"),
        )
    except oauth_clients.ClientConfigError as exc:
        return 400, {"error": str(exc)}

    try:
        token_data = oauth_providers.refresh_access_token(
            fields["provider"],
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
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
    # Refresh tokens issued by a client other than the shared one stay bound
    # to it: keep the explicit client credentials with the record so refresh
    # continues to work (tokens._client_override).
    stored = {
        key: fields[key] for key in ("client_id", "client_secret") if fields.get(key)
    }
    stored.update(oauth_providers.normalize_token_data(
        token_data, previous_refresh_token=refresh_token,
    ))
    put_credential(item["credential_id"], stored, provider=item["provider"])
    connections.put_connection(connections_table, item)
    audit.emit(item["connection_id"], audit.IMPORT, operator_subject, outcome="ok")
    return 200, connections.public_view(item)


def import_connection(event):
    subject, error = authenticate(event)
    if error:
        return error
    if not _is_operator(event, subject):
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
