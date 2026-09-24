"""End-to-end OAuth tests driven through the public entrypoints.

Unlike the handler-level tests, every request here enters through
``ingress.handler`` — the same dispatch API Gateway invokes — so routing,
authentication gates, CSRF checks, and the handlers are exercised together.
Identity tokens are real RS256 JWTs verified against a real key; only the
network edge (JWKS discovery, token endpoints) and DynamoDB are faked.
"""

import json
import time
import urllib.parse
import urllib.request
from types import SimpleNamespace

import boto3
import jwt
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives.asymmetric import rsa

from src import admin, agent_api, ingress

HOST = "dapier.example.test"
AUTH_BASE = "https://auth.example.test"
AUTH_CALLBACK = f"https://{HOST}/auth/callback"
ISSUER = f"{AUTH_BASE}/oauth2"
OIDC_CLIENT = "cognito-client"
CLI_CLIENT = "cli-client"
OAUTH_CALLBACK = f"https://{HOST}/oauth/callback"
EMAIL = "op@datatalks.club"
SUBJECT = "auth|111"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = PRIVATE_KEY.public_key()


def mint_token(**claims):
    now = int(time.time())
    payload = {
        "iss": ISSUER, "iat": now, "exp": now + 600,
        "aud": OIDC_CLIENT, "sub": SUBJECT,
        **claims,
    }
    return jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")


class FakeJWKClient:
    def __init__(self, url):
        pass

    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=PUBLIC_KEY)


class HttpFake:
    """Routes fake HTTP responses by URL substring; records every request."""

    def __init__(self):
        self.routes = []
        self.requests = []

    def on(self, substring, payload, status=200):
        self.routes.append((substring, payload, status))
        return self

    def response_for(self, url):
        for substring, payload, status in self.routes:
            if substring in url:
                return payload, status
        raise AssertionError(f"unexpected outbound request: {url}")

    def __call__(self, request, timeout=15):
        self.requests.append(request)
        payload, status = self.response_for(request.full_url)
        if callable(payload):
            payload = payload(request)
        return TokenResponse(payload, status)


class TokenResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeTable:
    def __init__(self, key_names):
        self.key_names = key_names
        self.items = {}

    def _key(self, mapping):
        return tuple(mapping[name] for name in self.key_names)

    def get_item(self, **kwargs):
        item = self.items.get(self._key(kwargs["Key"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        if kwargs.get("ConditionExpression") and self._key(item) in self.items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem",
            )
        self.items[self._key(item)] = dict(item)

    def scan(self, **kwargs):
        return {"Items": [dict(item) for item in self.items.values()]}


class FakeDynamo:
    def __init__(self):
        self.tables = {
            "connections": FakeTable(["connection_id"]),
            "executions": FakeTable(["execution_id"]),
            "grants": FakeTable(["connection_id", "grantee"]),
        }

    def Table(self, name):
        return self.tables[name]


def invoke(event):
    return ingress.handler(event, None)


def http_event(method, path, *, cookies=(), query=None, headers=None, body=None):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": HOST, **(headers or {})},
        "cookies": list(cookies),
    }
    if query is not None:
        event["queryStringParameters"] = query
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def cookie_value(response, prefix):
    for header in response.get("cookies") or []:
        if header.startswith(prefix):
            return header.split(";", 1)[0][len(prefix):]
    raise AssertionError(f"cookie {prefix} not in {response.get('cookies')}")


def configure(monkeypatch, http):
    dynamo = FakeDynamo()
    stored_credentials = {}

    monkeypatch.setenv("OPERATOR_EMAILS", EMAIL)
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "shared-client-id")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "shared-client-secret")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(admin, "_credentials", lambda: {"password": "session-secret"})
    monkeypatch.setattr(boto3, "resource", lambda service: dynamo)
    monkeypatch.setattr(urllib.request, "urlopen", http)
    monkeypatch.setattr(
        admin, "get_credential",
        lambda credential_id: dict(stored_credentials[credential_id]),
    )
    monkeypatch.setattr(
        admin, "put_credential",
        lambda credential_id, value, **kwargs: stored_credentials.__setitem__(
            credential_id, dict(value),
        ),
    )
    monkeypatch.setattr("jwt.PyJWKClient", FakeJWKClient)
    return dynamo, stored_credentials


def seed_youtube_connection(table, **overrides):
    item = {
        "connection_id": "youtube-personal",
        "provider": "youtube",
        "display_name": "Personal YouTube",
        "scopes": [YOUTUBE_SCOPE],
        "granted_scopes": [],
        "expected_account_id": None,
        "verified_account_id": None,
        "account_title": None,
        "credential_id": "oauth#youtube-personal",
        "status": "ready",
        "version": 1,
    }
    item.update(overrides)
    table.items[table._key(item)] = item
    return item


def sign_in(monkeypatch, http):
    """Walk the operator through /auth/login -> /auth/callback -> session."""
    monkeypatch.setenv("AUTH_BASE_URL", AUTH_BASE)
    monkeypatch.setenv("AUTH_CLIENT_ID", OIDC_CLIENT)
    monkeypatch.setenv("AUTH_CALLBACK_URL", AUTH_CALLBACK)
    monkeypatch.setenv("AUTH_ISSUER", ISSUER)
    minted = {}
    http.on(f"{AUTH_BASE}/oauth2/token", lambda request: {"id_token": minted["id_token"]})
    login = invoke(http_event("GET", "/auth/login"))
    assert login["statusCode"] == 302
    assert login["headers"]["location"].startswith(f"{AUTH_BASE}/oauth2/authorize?")
    state_cookie = cookie_value(login, "dapier_auth_state=")

    nonce = admin._verify(state_cookie, kind="oidc")["nonce"]
    minted["id_token"] = mint_token(nonce=nonce, email=EMAIL)

    from urllib.parse import parse_qs, urlparse

    authorize_query = parse_qs(urlparse(login["headers"]["location"]).query)
    callback = invoke(http_event(
        "GET", "/auth/callback",
        cookies=[f"dapier_auth_state={state_cookie}"],
        query={"code": "oidc-code", "state": authorize_query["state"][0]},
    ))
    assert callback["statusCode"] == 302, callback
    assert callback["headers"]["location"] == "/"
    return cookie_value(callback, "dapier_session=")


def test_operator_login_and_provider_oauth_end_to_end(monkeypatch):
    http = HttpFake()
    dynamo, stored = configure(monkeypatch, http)
    monkeypatch.setenv("OAUTH_CALLBACK_URL", OAUTH_CALLBACK)
    session = sign_in(monkeypatch, http)

    me = invoke(http_event(
        "GET", "/api/admin/me", cookies=[f"dapier_session={session}"],
    ))
    assert me["statusCode"] == 200
    assert json.loads(me["body"]) == {"username": EMAIL, "operator": True}

    seed_youtube_connection(dynamo.tables["connections"])

    anonymous = invoke(
        http_event("GET", "/api/admin/oauth/youtube-personal/start"))
    assert anonymous["statusCode"] == 401

    start = invoke(http_event(
        "GET", "/api/admin/oauth/youtube-personal/start",
        cookies=[f"dapier_session={session}"],
    ))
    assert start["statusCode"] == 302
    location = start["headers"]["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "code_challenge_method=S256" in location
    assert urllib.parse.quote(OAUTH_CALLBACK, safe="") in location
    oauth_state_cookie = cookie_value(start, "dapier_oauth_state=")

    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(location).query)["state"][0]
    http.on("oauth2.googleapis.com/token", {
        "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
        "scope": YOUTUBE_SCOPE,
    })
    http.on("youtube/v3/channels", {
        "items": [{"id": "UC1", "snippet": {"title": "Ch"}}],
    })
    callback = invoke(http_event(
        "GET", "/oauth/callback",
        cookies=[f"dapier_oauth_state={oauth_state_cookie}",
                 f"dapier_session={session}"],
        query={"code": "provider-code", "state": state},
    ))
    assert callback["statusCode"] == 302, callback
    assert callback["headers"]["location"] == "/?oauth=connected"

    credential = stored["oauth#youtube-personal"]
    assert credential["access_token"] == "at"
    assert credential["refresh_token"] == "rt"
    assert "client_secret" not in credential
    connected = dynamo.tables["connections"].items[("youtube-personal",)]
    assert connected["status"] == "connected"
    assert connected["verified_account_id"] == "UC1"
    assert connected["granted_scopes"] == [YOUTUBE_SCOPE]

    replay = invoke(http_event(
        "GET", "/oauth/callback",
        cookies=[f"dapier_oauth_state={oauth_state_cookie}",
                 f"dapier_session={session}"],
        query={"code": "provider-code", "state": state},
    ))
    assert replay["statusCode"] == 400

    forged = invoke(http_event(
        "GET", "/oauth/callback",
        cookies=[f"dapier_oauth_state={oauth_state_cookie}",
                 f"dapier_session={session}"],
        query={"code": "provider-code", "state": state[:-2] + "xx"},
    ))
    assert forged["statusCode"] == 400


def test_login_rejects_mismatched_state(monkeypatch):
    http = HttpFake()
    configure(monkeypatch, http)
    monkeypatch.setenv("AUTH_BASE_URL", AUTH_BASE)
    monkeypatch.setenv("AUTH_CLIENT_ID", OIDC_CLIENT)
    monkeypatch.setenv("AUTH_CALLBACK_URL", AUTH_CALLBACK)
    monkeypatch.setenv("AUTH_ISSUER", ISSUER)

    login = invoke(http_event("GET", "/auth/login"))
    state_cookie = cookie_value(login, "dapier_auth_state=")
    from urllib.parse import parse_qs, urlparse

    state = parse_qs(urlparse(login["headers"]["location"]).query)["state"][0]
    callback = invoke(http_event(
        "GET", "/auth/callback",
        cookies=[f"dapier_auth_state={state_cookie}"],
        query={"code": "oidc-code", "state": state[:-2] + "xx"},
    ))
    assert callback["statusCode"] == 303
    assert callback["headers"]["location"] == "/auth/error"


def test_agent_token_end_to_end(monkeypatch):
    http = HttpFake()
    dynamo, _stored = configure(monkeypatch, http)
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", CLI_CLIENT)
    monkeypatch.setenv("AUTH_ISSUER", ISSUER)
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    agent_api.reset_rate_limits()

    seed_youtube_connection(
        dynamo.tables["connections"],
        expected_account_id="UC1",
        verified_account_id="UC1",
        account_title="Ch",
        granted_scopes=[YOUTUBE_SCOPE],
        status="connected",
        version=2,
        updated_at="now",
        connected_at="now",
    )
    grants = dynamo.tables["grants"]
    grants.items[grants._key({
        "connection_id": "youtube-personal",
        "grantee": f"{SUBJECT}#uploader",
    })] = {
        "connection_id": "youtube-personal",
        "grantee": f"{SUBJECT}#uploader",
        "subject": SUBJECT,
        "agent": "uploader",
        "operations": ["use"],
    }
    monkeypatch.setattr(
        agent_api.tokens, "get_access_token",
        lambda connection: ("live-access", {
            "expires_at": int(time.time()) + 300, "scope": YOUTUBE_SCOPE,
            "provider_account_id": "UC1", "account_title": "Ch",
            "refreshed": False,
        }),
    )

    id_token = mint_token(aud=CLI_CLIENT)
    issue = invoke(http_event(
        "POST", "/api/agent/token",
        headers={"authorization": f"Bearer {id_token}"},
        body={"connection_id": "youtube-personal", "agent": "uploader"},
    ))
    assert issue["statusCode"] == 200, issue
    body = json.loads(issue["body"])
    assert body["access_token"] == "live-access"
    assert "refresh_token" not in json.dumps(body)
    assert issue["headers"]["cache-control"] == "no-store"

    impostor = invoke(http_event(
        "POST", "/api/agent/token",
        headers={"authorization": f"Bearer {mint_token(aud=CLI_CLIENT, sub='someone-else')}"},
        body={"connection_id": "youtube-personal", "agent": "uploader"},
    ))
    assert impostor["statusCode"] == 403

    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
        {"iss": ISSUER, "aud": CLI_CLIENT, "sub": SUBJECT,
         "iat": now, "exp": now + 600},
        other_key, algorithm="RS256",
    )
    denied = invoke(http_event(
        "POST", "/api/agent/token",
        headers={"authorization": f"Bearer {forged}"},
        body={"connection_id": "youtube-personal", "agent": "uploader"},
    ))
    assert denied["statusCode"] == 401


def test_unauthenticated_agent_token_is_rejected(monkeypatch):
    http = HttpFake()
    configure(monkeypatch, http)
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", CLI_CLIENT)
    agent_api.reset_rate_limits()
    response = invoke(http_event(
        "POST", "/api/agent/token",
        body={"connection_id": "x", "agent": "a"},
    ))
    assert response["statusCode"] == 401
