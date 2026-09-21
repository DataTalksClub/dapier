import json
import urllib.parse
from botocore.exceptions import ClientError

from src import admin

CALLBACK_URL = "https://fixed.example.test/oauth/callback"
YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


class ConnectionsTable:
    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        self.items[kwargs["Item"]["connection_id"]] = kwargs["Item"]

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["connection_id"])
        return {"Item": dict(item)} if item else {}


class ExecutionsTable:
    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        if kwargs.get("ConditionExpression") and kwargs["Item"]["execution_id"] in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[kwargs["Item"]["execution_id"]] = kwargs["Item"]


class TokenResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status = status

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def configure(monkeypatch, token_payload=None, token_status=200):
    connections = ConnectionsTable()
    executions = ExecutionsTable()
    stored = {}
    requests = []

    class Dynamo:
        def Table(self, name):
            if name == "connections":
                return connections
            return executions

    def fake_urlopen(request, timeout=15):
        requests.append(request)
        return TokenResponse(token_payload, status=token_status)

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("OAUTH_CALLBACK_URL", CALLBACK_URL)
    monkeypatch.setattr(admin, "_credentials", lambda: {"password": "session-secret"})
    monkeypatch.setattr(admin.boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(
        admin, "get_credential", lambda credential_id: dict(stored[credential_id]),
    )
    monkeypatch.setattr(
        admin, "put_credential",
        lambda credential_id, value, **kwargs: stored.__setitem__(credential_id, dict(value)),
    )
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return connections, executions, stored, requests


def seed_connection(connections, **overrides):
    item = {
        "connection_id": "youtube-personal",
        "provider": "youtube",
        "display_name": "Personal YouTube",
        "client_id": "client-id",
        "scopes": [YOUTUBE_SCOPE],
        "granted_scopes": [],
        "expected_account_id": None,
        "verified_account_id": None,
        "account_title": None,
        "owner_subject": "subject-1",
        "credential_id": "oauth#youtube-personal",
        "status": "ready",
        "version": 1,
    }
    item.update(overrides)
    connections.items[item["connection_id"]] = item
    return item


def session_cookie(subject="subject-1"):
    import time as _time

    token = admin._sign({"sub": "op@example.test", "subject": subject, "exp": int(_time.time()) + 3600})
    return f"dapier_session={token}"


def start_event(subject="subject-1", headers=None):
    return {
        "headers": headers or {"host": "fixed.example.test"},
        "cookies": [session_cookie(subject)],
    }


def start_flow(monkeypatch, connection_id="youtube-personal", **kwargs):
    connections, executions, stored, requests = configure(
        monkeypatch,
        token_payload={"access_token": "at", "refresh_token": "rt",
                       "expires_in": 3600, "scope": YOUTUBE_SCOPE},
    )
    seed_connection(connections)
    stored["oauth#youtube-personal"] = {"client_secret": "client-secret"}
    response = admin.oauth_start(start_event(**kwargs), connection_id)
    assert response["statusCode"] == 302
    return connections, executions, stored, requests, response


def callback_event(response, subject="subject-1"):
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(response["headers"]["location"]).query)
    state_cookie = response["cookies"][0].split(";", 1)[0]
    return {
        "cookies": [state_cookie, session_cookie(subject)],
        "queryStringParameters": {"code": "auth-code", "state": query["state"][0]},
    }


def test_start_uses_fixed_redirect_and_pkce(monkeypatch):
    _, _, _, _, response = start_flow(
        monkeypatch, headers={"host": "fixed.example.test", "x-forwarded-host": "evil.example.test"},
    )
    location = response["headers"]["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert urllib.parse.quote(CALLBACK_URL, safe="") in location
    assert "evil.example.test" not in location
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location


def test_start_requires_configured_callback(monkeypatch):
    connections, _, _, _ = configure(monkeypatch)[:4]
    seed_connection(connections)
    monkeypatch.delenv("OAUTH_CALLBACK_URL")
    response = admin.oauth_start(start_event(), "youtube-personal")
    assert response["statusCode"] == 503


def test_callback_success_is_single_use(monkeypatch):
    connections, executions, stored, requests, response = start_flow(monkeypatch)
    event = callback_event(response)

    first = admin.oauth_callback(event)
    assert first["statusCode"] == 302
    assert first["headers"]["location"] == "/?oauth=connected"

    token_request = requests[0]
    sent = token_request.data.decode()
    assert "code_verifier=" in sent
    assert f"redirect_uri={urllib.parse.quote(CALLBACK_URL, safe='')}" in sent

    credential = stored["oauth#youtube-personal"]
    assert credential["access_token"] == "at"
    assert credential["refresh_token"] == "rt"
    assert credential["client_secret"] == "client-secret"
    assert "tokens" not in credential

    updated = connections.items["youtube-personal"]
    assert updated["status"] == "connected"
    assert updated["granted_scopes"] == [YOUTUBE_SCOPE]
    assert updated["connected_by"] == "subject-1"
    assert any(key.startswith("oauth-state:") for key in executions.items)

    replay = admin.oauth_callback(event)
    assert replay["statusCode"] == 400
    assert json.loads(replay["body"])["error"] == "Invalid or expired OAuth state"


def test_callback_rejects_wrong_operator(monkeypatch):
    _, _, _, _, response = start_flow(monkeypatch)
    event = callback_event(response, subject="subject-2")
    result = admin.oauth_callback(event)
    assert result["statusCode"] == 400
    assert "does not match" in json.loads(result["body"])["error"]


def test_callback_ignores_request_host(monkeypatch):
    _, _, stored, _, response = start_flow(monkeypatch)
    event = callback_event(response)
    event["headers"] = {"host": "evil.example.test", "x-forwarded-host": "evil.example.test"}
    result = admin.oauth_callback(event)
    assert result["statusCode"] == 302
    assert stored["oauth#youtube-personal"]["access_token"] == "at"


def test_callback_provider_error_stores_nothing(monkeypatch):
    connections, _, stored, _ = configure(monkeypatch, token_payload={"error": "invalid_grant"}, token_status=400)
    seed_connection(connections)
    stored["oauth#youtube-personal"] = {"client_secret": "client-secret"}
    response = admin.oauth_start(start_event(), "youtube-personal")
    result = admin.oauth_callback(callback_event(response))
    assert result["statusCode"] == 400
    assert stored["oauth#youtube-personal"] == {"client_secret": "client-secret"}
    assert connections.items["youtube-personal"]["status"] == "ready"


def test_callback_rejects_missing_scopes(monkeypatch):
    connections, _, _, _ = configure(
        monkeypatch,
        token_payload={"access_token": "at", "refresh_token": "rt",
                       "expires_in": 3600, "scope": "https://www.googleapis.com/auth/youtube.upload"},
    )
    seed_connection(connections)
    import copy

    stored = {"oauth#youtube-personal": {"client_secret": "client-secret"}}
    monkeypatch.setattr(admin, "get_credential", lambda credential_id: copy.deepcopy(stored[credential_id]))
    writes = []
    monkeypatch.setattr(
        admin, "put_credential",
        lambda credential_id, value, **kwargs: writes.append(credential_id),
    )
    response = admin.oauth_start(start_event(), "youtube-personal")
    result = admin.oauth_callback(callback_event(response))
    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert body["missing_scopes"] == [YOUTUBE_SCOPE]
    assert writes == []
    assert connections.items["youtube-personal"]["status"] == "ready"


def test_callback_rejects_expired_state(monkeypatch):
    import time as _time

    configure(monkeypatch)
    stale = admin._sign({"kind": "oauth", "connection_id": "x", "jti": "abc",
                         "redirect_uri": CALLBACK_URL, "exp": int(_time.time()) - 1})
    result = admin.oauth_callback({
        "cookies": [f"dapier_oauth_state={stale}"],
        "queryStringParameters": {"code": "c", "state": stale},
    })
    assert result["statusCode"] == 400
