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

from .credentials import credential_status, get_credential, put_credential


SESSION_COOKIE = "dapier_session"
OAUTH_COOKIE = "dapier_oauth_state"
AUTH_STATE_COOKIE = "dapier_auth_state"
SESSION_TTL_SECONDS = 12 * 60 * 60
CREDENTIAL_SPECS = {
    "slack": {"credential_id": "slack", "fields": ("token",)},
    "mailchimp": {"credential_id": "mailchimp", "fields": ("api_key",)},
}
OAUTH_PROVIDERS = {
    "dropbox": {
        "authorization_url": "https://www.dropbox.com/oauth2/authorize",
        "token_url": "https://api.dropboxapi.com/oauth2/token",
        "extra": {"token_access_type": "offline"},
    },
    "youtube": {
        "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "extra": {"access_type": "offline", "prompt": "consent"},
    },
}

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


def authenticated(event):
    return _verify(_cookie(event, SESSION_COOKIE)) is not None


def _auth_config():
    issuer = os.environ.get("AUTH_ISSUER", "").rstrip("/")
    return {
        "base_url": os.environ.get("AUTH_BASE_URL", "").rstrip("/"),
        "client_id": os.environ.get("AUTH_CLIENT_ID", ""),
        "callback_url": os.environ.get("AUTH_CALLBACK_URL", ""),
        "logout_url": os.environ.get("AUTH_LOGOUT_URL", ""),
        "issuer": issuer,
        "jwks_url": os.environ.get("AUTH_JWKS_URL", f"{issuer}/.well-known/jwks.json" if issuer else ""),
    }


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


def _exchange_auth_code(code, verifier):
    config = _auth_config()
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "client_id": config["client_id"],
        "code": code, "redirect_uri": config["callback_url"], "code_verifier": verifier,
    }).encode()
    request = urllib.request.Request(
        f'{config["base_url"]}/oauth2/token', data=body,
        headers={"content-type": "application/x-www-form-urlencoded"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read())


def _verify_id_token(id_token):
    import jwt

    config = _auth_config()
    key = jwt.PyJWKClient(config["jwks_url"]).get_signing_key_from_jwt(id_token)
    return jwt.decode(
        id_token, key.key, algorithms=["RS256"], audience=config["client_id"],
        issuer=config["issuer"], options={"require": ["exp", "iat", "iss", "aud", "sub"]},
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
    email = claims.get("email")
    if not isinstance(email, str) or claims.get("email_verified") is not True:
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
    connection_id = str(body.get("connection_id", "")).strip().lower()
    provider = str(body.get("provider", "")).strip().lower()
    client_id = str(body.get("client_id", "")).strip()
    client_secret = str(body.get("client_secret", "")).strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,62}", connection_id):
        return _json_response(400, {"error": "Connection ID must use lowercase letters, numbers, dashes, or underscores"})
    if provider not in OAUTH_PROVIDERS or not client_id or not client_secret:
        return _json_response(400, {"error": "Provider, client ID, and client secret are required"})

    credential_id = f"oauth#{connection_id}"
    put_credential(credential_id, {"client_secret": client_secret}, provider=provider)

    now = datetime.now(timezone.utc).isoformat()
    item = {
        "connection_id": connection_id,
        "provider": provider,
        "display_name": str(body.get("display_name") or connection_id).strip()[:100],
        "client_id": client_id,
        "scopes": [scope for scope in body.get("scopes", []) if isinstance(scope, str)],
        "credential_id": credential_id,
        "status": "ready",
        "updated_at": now,
    }
    boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).put_item(Item=item)
    return _json_response(200, item)


def _connection(connection_id):
    return boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).get_item(
        Key={"connection_id": connection_id}
    ).get("Item")


def oauth_start(event, connection_id):
    connection = _connection(connection_id)
    if not connection:
        return _json_response(404, {"error": "Connection not found"})
    provider = OAUTH_PROVIDERS[connection["provider"]]
    host = _header(event, "x-forwarded-host") or _header(event, "host")
    redirect_uri = f"https://{host}/oauth/callback"
    state = _sign({
        "kind": "oauth",
        "connection_id": connection_id,
        "redirect_uri": redirect_uri,
        "exp": int(time.time()) + 600,
    })
    params = {
        "client_id": connection["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
        **provider["extra"],
    }
    if connection.get("scopes"):
        params["scope"] = " ".join(connection["scopes"])
    return _redirect(
        f"{provider['authorization_url']}?{urllib.parse.urlencode(params)}",
        cookies=[f"{OAUTH_COOKIE}={state}; Path=/oauth/callback; Max-Age=600; HttpOnly; Secure; SameSite=Lax"],
    )


def oauth_callback(event):
    query = event.get("queryStringParameters") or {}
    state = query.get("state", "")
    payload = _verify(state, "oauth")
    if not payload or not hmac.compare_digest(state, _cookie(event, OAUTH_COOKIE)):
        return _json_response(400, {"error": "Invalid or expired OAuth state"})
    if query.get("error"):
        return _redirect(f"/?oauth={urllib.parse.quote(query['error'])}")
    connection = _connection(payload["connection_id"])
    if not connection or not query.get("code"):
        return _json_response(400, {"error": "OAuth connection or code is missing"})

    secret = get_credential(connection["credential_id"])
    provider = OAUTH_PROVIDERS[connection["provider"]]
    request = urllib.request.Request(
        provider["token_url"],
        data=urllib.parse.urlencode({
            "code": query["code"],
            "grant_type": "authorization_code",
            "client_id": connection["client_id"],
            "client_secret": secret["client_secret"],
            "redirect_uri": payload["redirect_uri"],
        }).encode(),
        headers={"content-type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        tokens = json.loads(response.read())
    secret["tokens"] = tokens
    put_credential(connection["credential_id"], secret, provider=connection["provider"])
    connection.update({"status": "connected", "connected_at": datetime.now(timezone.utc).isoformat()})
    boto3.resource("dynamodb").Table(os.environ["CONNECTIONS_TABLE"]).put_item(Item=connection)
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
    if not authenticated(event):
        return _json_response(401, {"error": "Authentication required"})
    if method == "GET" and path == "/api/admin/me":
        return _json_response(200, {"username": _verify(_cookie(event, SESSION_COOKIE))["sub"]})
    if method == "POST" and path == "/api/admin/logout":
        return logout()
    if method == "GET" and path == "/api/admin/overview":
        return overview()
    if method == "PUT" and path.startswith("/api/admin/credentials/"):
        return save_credential(path.rsplit("/", 1)[1], event)
    if method == "PUT" and path == "/api/admin/connections":
        return save_connection(event)
    match = re.fullmatch(r"/api/admin/oauth/([a-z0-9_-]+)/start", path)
    if method == "GET" and match:
        return oauth_start(event, match.group(1))
    return _json_response(404, {"error": "Not found"})
