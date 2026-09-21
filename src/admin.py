import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3
import yaml

from . import agent_api
from . import audit as audit_log
from . import authz
from . import connections as connection_model
from . import oauth_providers
from .credentials import credential_status, get_credential, put_credential
from .dtc_auth import auth_config as _auth_config
from .dtc_auth import exchange_auth_code as _exchange_auth_code
from .dtc_auth import verify_id_token as _verify_id_token


SESSION_COOKIE = "dapier_session"
OAUTH_COOKIE = "dapier_oauth_state"
AUTH_STATE_COOKIE = "dapier_auth_state"
SESSION_TTL_SECONDS = 12 * 60 * 60
CREDENTIAL_SPECS = {
    "slack": {"credential_id": "slack", "fields": ("token",)},
    "mailchimp": {"credential_id": "mailchimp", "fields": ("api_key",)},
}
OAUTH_PROVIDERS = oauth_providers.PROVIDERS

_admin_secret = None


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_response(status, body, *, cookies=None, headers=None):
    response = {
        "statusCode": status,
        "headers": {"content-type": "application/json", **(headers or {})},
        "body": json.dumps(body, default=_json_default),
    }
    if cookies:
        response["cookies"] = cookies
    return response


def _redirect(location, *, cookies=None, status=302, headers=None):
    response = {
        "statusCode": status,
        "headers": {"location": location, **(headers or {})},
        "body": "",
    }
    if cookies:
        response["cookies"] = cookies
    return response


def _b64encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _credentials():
    global _admin_secret
    if _admin_secret is None:
        value = boto3.client("secretsmanager").get_secret_value(
            SecretId=os.environ["ADMIN_SECRET_ID"]
        )["SecretString"]
        _admin_secret = json.loads(value)
    return _admin_secret


def _sign(payload):
    encoded = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = hmac.new(_credentials()["password"].encode(), encoded.encode(), hashlib.sha256).digest()
    return f"{encoded}.{_b64encode(signature)}"


def _verify(token, kind="session"):
    try:
        encoded, supplied = token.split(".", 1)
        expected = hmac.new(
            _credentials()["password"].encode(), encoded.encode(), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(_b64decode(supplied), expected):
            return None
        payload = json.loads(_b64decode(encoded))
        if payload.get("kind", "session") != kind or payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _cookie(event, name):
    cookies = event.get("cookies") or []
    if not cookies:
        raw = _header(event, "cookie")
        cookies = [part.strip() for part in raw.split(";") if part.strip()]
    prefix = f"{name}="
    return next((part[len(prefix):] for part in cookies if part.startswith(prefix)), "")


def _header(event, name):
    expected = name.lower()
    return next(
        (str(value) for key, value in (event.get("headers") or {}).items() if key.lower() == expected),
        "",
    )


def _session_payload(event):
    return _verify(_cookie(event, SESSION_COOKIE)) or {}


def authenticated(event):
    return _verify(_cookie(event, SESSION_COOKIE)) is not None


def require_operator(event):
    """Return ``(payload, None)`` for operators, ``(None, response)`` otherwise."""
    payload = _session_payload(event)
    if not payload:
        return None, _json_response(401, {"error": "Authentication required"})
    if not authz.is_operator(payload):
        _audit_event(
            "unknown", audit_log.CONNECT, subject_fallback(payload),
            outcome="denied-not-operator",
        )
        return None, _json_response(403, {"error": "Operator authorization required"})
    return payload, None


def subject_fallback(payload):
    subject = (payload or {}).get("subject") or (payload or {}).get("sub")
    return str(subject) if subject else "unknown"


def _audit_event(connection_id, action, actor, *, outcome, agent=None, error=None):
    return audit_log.emit(
        connection_id, action, actor, outcome=outcome, agent=agent, error=error,
    )


def _csrf_ok(event, method):
    """Same-origin check for cookie-authenticated state-changing requests."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    if not _cookie(event, SESSION_COOKIE):
        return True
    host = _header(event, "host").lower()
    for header in (_header(event, "origin"), _header(event, "referer")):
        if not header:
            continue
        try:
            if urllib.parse.urlparse(header).hostname.lower() == host:
                return True
        except ValueError:
            continue
    return False


def _session_subject(event):
    payload = _session_payload(event)
    subject = payload.get("subject") or payload.get("sub")
    return str(subject) if subject else None


def auth_login(event):
    config = _auth_config()
    if not all(config[key] for key in ("base_url", "client_id", "callback_url", "issuer", "jwks_url")):
        return _json_response(503, {"error": "Shared authentication is not configured"})
    state = _b64encode(os.urandom(32))
    verifier = _b64encode(os.urandom(48))
    nonce = _b64encode(os.urandom(32))
    challenge = _b64encode(hashlib.sha256(verifier.encode()).digest())
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": config["client_id"],
        "redirect_uri": config["callback_url"], "scope": "openid email profile",
        "state": state, "nonce": nonce, "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    token = _sign({
        "kind": "oidc", "state": state, "verifier": verifier, "nonce": nonce,
        "exp": int(time.time()) + 600,
    })
    return _redirect(
        f'{config["base_url"]}/oauth2/authorize?{query}',
        cookies=[f"{AUTH_STATE_COOKIE}={token}; Path=/auth/callback; Max-Age=600; HttpOnly; Secure; SameSite=Lax"],
    )


def auth_callback(event):
    pending = _verify(_cookie(event, AUTH_STATE_COOKIE), kind="oidc")
    query = event.get("queryStringParameters") or {}
    code, state = query.get("code", ""), query.get("state", "")
    clear_state = f"{AUTH_STATE_COOKIE}=; Path=/auth/callback; Max-Age=0; HttpOnly; Secure; SameSite=Lax"
    if not pending or not code or not hmac.compare_digest(state, pending.get("state", "")):
        return _auth_error_redirect(clear_state)
    try:
        claims = _verify_id_token(_exchange_auth_code(code, pending["verifier"])["id_token"])
    except Exception:
        return _auth_error_redirect(clear_state)
    if not hmac.compare_digest(str(claims.get("nonce", "")), pending.get("nonce", "")):
        return _auth_error_redirect(clear_state)
    # `email_verified` is not re-checked here: the shared pool's pre-sign-up trigger
    # already requires a verified @datatalks.club address on every Google
    # authentication, and this app client is Google-only. The email claim itself is
    # still required, because the session is keyed by it.
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        return _auth_error_redirect(clear_state)
    token = _sign({"sub": email.lower(), "subject": claims["sub"], "exp": int(time.time()) + SESSION_TTL_SECONDS})
    return _redirect(
        "/",
        cookies=[clear_state, f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; Secure; SameSite=Lax"],
    )


def _auth_error_redirect(clear_state):
    return _redirect(
        "/auth/error",
        status=303,
        cookies=[clear_state],
        headers={"cache-control": "no-store", "referrer-policy": "no-referrer"},
    )


def auth_error():
    return {
        "statusCode": 403,
        "headers": {
            "content-type": "text/html; charset=utf-8",
            "cache-control": "no-store",
            "referrer-policy": "no-referrer",
            "x-content-type-options": "nosniff",
            "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'",
        },
        "body": """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sign-in error · Dapier</title><style>body{margin:0;font:16px system-ui;background:#f5f7fa;color:#172033}main{max-width:32rem;margin:10vh auto;padding:2rem;background:white;border:1px solid #dce2ea;border-radius:.75rem}a{color:#1769aa;font-weight:600}</style></head><body><main><h1>Sign-in error</h1><p>Authentication could not be completed. No account changes were made.</p><p><a href="/auth/login">Try again</a></p></main></body></html>""",
    }


def auth_logout():
    config = _auth_config()
    location = "/"
    if config["base_url"] and config["client_id"] and config["logout_url"]:
        query = urllib.parse.urlencode({"client_id": config["client_id"], "logout_uri": config["logout_url"]})
        location = f'{config["base_url"]}/logout?{query}'
    return _redirect(location, cookies=[f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax"])


def _request_json(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def login(event):
    try:
        body = _request_json(event)
    except (ValueError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    credentials = _credentials()
    username = str(body.get("username", ""))
    password = str(body.get("password", ""))
    if not (
        hmac.compare_digest(username, credentials.get("username", "admin"))
        and hmac.compare_digest(password, credentials["password"])
    ):
        return _json_response(401, {"error": "Invalid username or password"})
    token = _sign({"sub": username, "exp": int(time.time()) + SESSION_TTL_SECONDS})
    return _json_response(
        200,
        {"username": username},
        cookies=[f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; Secure; SameSite=Strict"],
    )


def logout():
    return _json_response(
        200,
        {"ok": True},
        cookies=[f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict"],
    )


def _workflows():
    root = Path(os.environ.get("WORKFLOWS_DIR", Path(__file__).parent.parent / "workflows"))
    result = []
    for path in sorted(root.glob("*.yaml")):
        workflow = yaml.safe_load(path.read_text())
        result.append({
            "id": workflow["id"],
            "enabled": workflow.get("enabled", True),
            "trigger": workflow["trigger"],
            "source": path.name,
            "actions": [
                {**action, "id": action.get("id", str(index)), "type": action["type"]}
                for index, action in enumerate(workflow.get("actions", []))
            ],
        })
    return result


def _workflows_edit_base():
    return os.environ.get(
        "WORKFLOWS_REPO_URL", "https://github.com/DataTalksClub/dapier"
    ).rstrip("/") + "/edit/main/workflows"


def _scan(table_name, limit=50):
    table = boto3.resource("dynamodb").Table(table_name)
    return table.scan(Limit=limit).get("Items", [])


def _credential_status(provider):
    spec = CREDENTIAL_SPECS[provider]
    return {"provider": provider, **credential_status(spec["credential_id"])}


def overview():
    executions = sorted(
        _scan(os.environ["EXECUTIONS_TABLE"]),
        key=lambda item: item.get("execution_id", ""),
        reverse=True,
    )
    connections = _scan(os.environ["CONNECTIONS_TABLE"])
    return _json_response(200, {
        "service": "dapier",
        "region": os.environ.get("AWS_REGION", "eu-west-1"),
        "workflows": _workflows(),
        "workflows_edit_base": _workflows_edit_base(),
        "executions": executions[:25],
        "connections": sorted(connections, key=lambda item: item.get("display_name", "")),
        "credentials": [_credential_status(provider) for provider in CREDENTIAL_SPECS],
    })


def save_credential(provider, event):
    if provider not in CREDENTIAL_SPECS:
        return _json_response(404, {"error": "Unknown credential provider"})
    try:
        body = _request_json(event)
    except (ValueError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})

    if provider == "slack":
        token = str(body.get("token", "")).strip()
        if not token.startswith(("xoxb-", "xapp-")) or len(token) < 20:
            return _json_response(400, {"error": "Enter a valid Slack bot token"})
        secret_value = {"token": token}
    else:
        api_key = str(body.get("api_key", "")).strip()
        match = re.fullmatch(r"[A-Za-z0-9_-]{20,}-us\d{1,3}", api_key)
        if not match:
            return _json_response(400, {"error": "Enter a valid Mailchimp API key"})
        secret_value = {"apiKey": api_key, "server": api_key.rsplit("-", 1)[1]}

    put_credential(CREDENTIAL_SPECS[provider]["credential_id"], secret_value, provider=provider)
    return _json_response(200, {"provider": provider, "configured": True})


def save_connection(event):
    try:
        body = _request_json(event)
    except (ValueError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    try:
        fields = connection_model.validate_new_connection(body)
    except connection_model.ConnectionError as exc:
        return _json_response(400, {"error": str(exc)})

    connections_table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    previous = connection_model.get_connection(connections_table, fields["connection_id"])
    operator = _session_subject(event)
    try:
        item = connection_model.build_item(
            fields, owner_subject=operator, previous=previous,
        )
    except connection_model.BindingError as exc:
        _audit_event(fields["connection_id"], audit_log.CONNECT, operator or "unknown",
                     outcome="error", error=str(exc))
        return _json_response(409, {"error": str(exc)})

    put_credential(item["credential_id"], {"client_secret": fields["client_secret"]}, provider=item["provider"])
    connection_model.put_connection(connections_table, item)
    _audit_event(item["connection_id"], audit_log.CONNECT, operator or "unknown", outcome="ok")
    return _json_response(200, item)


def list_grants(event):
    query = event.get("queryStringParameters") or {}
    table = authz.grants_table()
    items = authz.list_grants(table, connection_id=query.get("connection_id") or None)
    return _json_response(200, {"grants": [
        {key: item.get(key) for key in (
            "connection_id", "grantee", "subject", "agent", "operations",
            "granted_by", "granted_at", "updated_at", "expires_at",
        )} for item in items
    ]})


def save_grant(event, operator):
    try:
        body = _request_json(event)
    except (ValueError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    connection_id = str(body.get("connection_id", "")).strip().lower()
    subject = str(body.get("subject", "")).strip()
    if not connection_id or not subject:
        return _json_response(400, {"error": "Connection ID and subject are required"})
    if not _connection(connection_id):
        return _json_response(404, {"error": "Connection not found"})
    try:
        item = authz.put_grant(
            authz.grants_table(),
            connection_id=connection_id,
            subject=subject,
            agent=body.get("agent", ""),
            operations=body.get("operations", []),
            granted_by=operator,
            expires_at=body.get("expires_at"),
        )
    except ValueError as exc:
        return _json_response(400, {"error": str(exc)})
    _audit_event(connection_id, audit_log.GRANT, operator,
                 agent=item["agent"], outcome="ok")
    return _json_response(200, item)


def delete_grant(event, operator):
    query = event.get("queryStringParameters") or {}
    connection_id = str(query.get("connection_id", "")).strip().lower()
    grantee_id = str(query.get("grantee", "")).strip()
    if not connection_id or not grantee_id:
        return _json_response(400, {"error": "Connection ID and grantee are required"})
    authz.delete_grant(authz.grants_table(), connection_id=connection_id, grantee_id=grantee_id)
    _audit_event(connection_id, audit_log.GRANT, operator, outcome="revoked")
    return _json_response(200, {"ok": True})


def _connection(connection_id):
    return connection_model.get_connection(
        boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]),
        connection_id,
    )


def import_connection(event, operator):
    """One-time operator import of an existing provider credential (cookie path)."""
    try:
        body = _request_json(event)
    except (ValueError, json.JSONDecodeError):
        return _json_response(400, {"error": "Invalid request"})
    connections_table = boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"])
    status, payload = agent_api.import_core(body, operator_subject=operator, connections_table=connections_table)
    return _json_response(status, payload)


def revoke_connection_tokens(connection_id, operator):
    from . import tokens as token_lifecycle

    connection = _connection(connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    updated = token_lifecycle.revoke_connection(connection)
    boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).put_item(Item=updated)
    _audit_event(connection_id, audit_log.REVOKE, operator, outcome="ok")
    return _json_response(200, {"connection_id": connection_id, "status": updated["status"]})


def oauth_callback_url():
    """The single registered provider redirect URI. Never derived from headers."""
    url = os.environ.get("OAUTH_CALLBACK_URL", "").strip()
    return url


def _claim_oauth_state(jti):
    """Consume an OAuth state ID exactly once. Returns False on replay."""
    from botocore.exceptions import ClientError

    table = boto3.resource("dynamodb").Table(os.environ["EXECUTIONS_TABLE"])
    try:
        table.put_item(
            Item={
                "execution_id": f"oauth-state:{jti}",
                "status": "consumed",
                "expires_at": int(time.time()) + 3600,
            },
            ConditionExpression="attribute_not_exists(execution_id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def oauth_start(event, connection_id):
    connection = _connection(connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    redirect_uri = oauth_callback_url()
    if not redirect_uri:
        return _json_response(503, {"error": "OAuth callback URL is not configured"})
    provider_name = connection["provider"]
    try:
        scopes = oauth_providers.normalize_scopes(provider_name, connection.get("scopes"))
    except oauth_providers.ProviderError as exc:
        return _json_response(400, {"error": str(exc)})
    verifier = _b64encode(os.urandom(48))
    challenge = _b64encode(hashlib.sha256(verifier.encode()).digest())
    state = _sign({
        "kind": "oauth",
        "connection_id": connection_id,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "jti": _b64encode(os.urandom(16)),
        "operator_subject": _session_subject(event),
        "exp": int(time.time()) + 600,
    })
    location = oauth_providers.authorization_url(
        provider_name,
        client_id=connection["client_id"],
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
        code_challenge=challenge,
    )
    return _redirect(
        location,
        cookies=[f"{OAUTH_COOKIE}={state}; Path=/oauth/callback; Max-Age=600; HttpOnly; Secure; SameSite=Lax"],
    )


def oauth_callback(event):
    query = event.get("queryStringParameters") or {}
    state = query.get("state", "")
    payload = _verify(state, "oauth")
    if not payload:
        return _json_response(400, {"error": "Invalid or expired OAuth state"})
    cookie_state = _cookie(event, OAUTH_COOKIE)
    if cookie_state:
        if not hmac.compare_digest(state, cookie_state):
            return _json_response(400, {"error": "Invalid or expired OAuth state"})
    elif not payload.get("operator_subject") or not payload.get("jti"):
        # Cookieless callbacks only for CLI-initiated flows, where the signed
        # state binds the operator subject and is single-use. Browser flows
        # always carry the state cookie set by oauth_start.
        return _json_response(400, {"error": "Invalid or expired OAuth state"})
    if query.get("error"):
        return _redirect(f"/?oauth={urllib.parse.quote(query['error'])}")
    if not payload.get("jti") or not _claim_oauth_state(payload["jti"]):
        return _json_response(400, {"error": "Invalid or expired OAuth state"})
    session_subject = _session_subject(event)
    operator = payload.get("operator_subject") or session_subject or "unknown"
    if session_subject and payload.get("operator_subject") != session_subject:
        _audit_event(payload.get("connection_id", "unknown"), audit_log.CALLBACK,
                     session_subject, outcome="denied-wrong-operator")
        return _json_response(400, {"error": "OAuth session does not match the connection request"})
    connection = _connection(payload["connection_id"])
    if not connection or not query.get("code"):
        return _json_response(400, {"error": "OAuth connection or code is missing"})

    try:
        previous = get_credential(connection["credential_id"])
    except KeyError:
        return _json_response(400, {"error": "OAuth connection credentials are missing"})
    try:
        token_data = oauth_providers.exchange_code(
            connection["provider"],
            code=query["code"],
            client_id=connection["client_id"],
            client_secret=previous["client_secret"],
            redirect_uri=payload["redirect_uri"],
            code_verifier=payload.get("code_verifier"),
        )
    except (oauth_providers.ProviderError, KeyError) as exc:
        message = str(exc) if isinstance(exc, oauth_providers.ProviderError) else "OAuth connection credentials are missing"
        _audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                     outcome="error", error=message)
        return _json_response(400, {"error": message})
    requested = set(connection.get("scopes") or [])
    granted_raw = token_data.get("scope")
    if granted_raw:
        granted = set(str(granted_raw).split())
        missing = sorted(requested - granted)
        if missing:
            _audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                         outcome="error", error="missing scopes")
            return _json_response(400, {
                "error": "Provider did not grant the requested scopes",
                "missing_scopes": missing,
            })
    else:
        granted = requested
    try:
        account_id, account_title = oauth_providers.verify_account(
            connection["provider"], token_data["access_token"],
        )
    except oauth_providers.ProviderError as exc:
        _audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                     outcome="error", error=str(exc))
        return _json_response(400, {"error": f"Could not verify the provider account: {exc}"})
    try:
        connection_model.check_binding(connection, account_id)
    except connection_model.BindingError as exc:
        _audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                     outcome="denied-account-mismatch", error=str(exc))
        return _json_response(409, {"error": str(exc)})
    stored = {
        "client_secret": previous.get("client_secret"),
        **oauth_providers.normalize_token_data(
            token_data, previous_refresh_token=previous.get("refresh_token"),
        ),
    }
    put_credential(connection["credential_id"], stored, provider=connection["provider"])
    try:
        updated = connection_model.mark_connected(
            connection,
            verified_account_id=account_id,
            account_title=account_title,
            granted_scopes=sorted(granted),
            connected_by=operator,
        )
    except connection_model.BindingError as exc:
        _audit_event(connection["connection_id"], audit_log.CALLBACK, operator,
                     outcome="error", error=str(exc))
        return _json_response(409, {"error": str(exc)})
    boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).put_item(Item=updated)
    _audit_event(connection["connection_id"], audit_log.CALLBACK, operator, outcome="ok")
    return _redirect(
        "/?oauth=connected",
        cookies=[f"{OAUTH_COOKIE}=; Path=/oauth/callback; Max-Age=0; HttpOnly; Secure; SameSite=Lax"],
    )


def route(event, method, path):
    if method == "GET" and path == "/auth/login":
        return auth_login(event)
    if method == "GET" and path == "/auth/callback":
        return auth_callback(event)
    if method == "GET" and path == "/auth/error":
        return auth_error()
    if method == "GET" and path == "/auth/logout":
        return auth_logout()
    if method == "POST" and path == "/api/admin/session" and os.environ.get("LEGACY_ADMIN_LOGIN_ENABLED", "").lower() in ("1", "true", "yes"):
        return login(event)
    if method == "GET" and path == "/oauth/callback":
        return oauth_callback(event)
    if path.startswith("/api/agent/"):
        return agent_api.route(event, method, path)
    if not authenticated(event):
        return _json_response(401, {"error": "Authentication required"})
    if method == "GET" and path == "/api/admin/me":
        payload = _session_payload(event)
        return _json_response(200, {
            "username": payload.get("sub"),
            "operator": authz.is_operator(payload),
        })
    if method == "POST" and path == "/api/admin/logout":
        return logout()
    if not _csrf_ok(event, method):
        return _json_response(403, {"error": "Cross-site request rejected"})
    operator_payload, operator_error = require_operator(event)
    if operator_error:
        return operator_error
    operator_subject = subject_fallback(operator_payload)
    if method == "GET" and path == "/api/admin/overview":
        return overview()
    if method == "PUT" and path.startswith("/api/admin/credentials/"):
        return save_credential(path.rsplit("/", 1)[1], event)
    if method == "PUT" and path == "/api/admin/connections":
        return save_connection(event)
    if method == "POST" and path == "/api/admin/connections/import":
        return import_connection(event, operator_subject)
    if method == "GET" and path == "/api/admin/grants":
        return list_grants(event)
    if method == "PUT" and path == "/api/admin/grants":
        return save_grant(event, operator_subject)
    if method == "DELETE" and path == "/api/admin/grants":
        return delete_grant(event, operator_subject)
    match = re.fullmatch(r"/api/admin/oauth/([a-z0-9_-]+)/start", path)
    if method == "GET" and match:
        return oauth_start(event, match.group(1))
    revoke_match = re.fullmatch(r"/api/admin/connections/([a-z0-9_-]+)/tokens", path)
    if method == "DELETE" and revoke_match:
        return revoke_connection_tokens(revoke_match.group(1), operator_subject)
    return _json_response(404, {"error": "Not found"})
