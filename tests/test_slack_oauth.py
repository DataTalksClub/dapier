"""Slack user-token OAuth: authorize URL, code exchange, connect flow.

Slack connects through OAuth v2 user scopes (``user_scope``) like every
other OAuth provider now; the pasted bot-token path stays for legacy
connections. The stored credential is the ``authed_user.access_token``
(``xoxp-``) from ``oauth.v2.access`` — never the bot token.
"""

import json
import urllib.parse

import pytest

from src.dapier.connections import importing, records, tokens
from src.dapier.connections.providers import oauth_providers
from src.dapier.connections.providers.oauth_providers import (
    ProviderError,
    authorization_url,
    exchange_code,
    normalize_scopes,
    refresh_access_token,
    revoke_token,
    verify_account,
)

CALLBACK_URL = "https://fixed.example.test/oauth/callback"
USER_SCOPES = ["im:history", "im:write", "users:read", "search:read"]

SLACK_TOKEN_PAYLOAD = {
    "ok": True,
    "access_token": "xoxb-bot-token",
    "token_type": "bot",
    "scope": "chat:write",
    "bot_user_id": "B1",
    "app_id": "A1",
    "team": {"id": "T012345", "name": "DataTalks"},
    "authed_user": {
        "id": "U01PAASPXHU",
        "scope": "im:history,im:write,users:read,search:read",
        "access_token": "xoxp-user-token",
        "token_type": "user",
    },
}

AUTH_TEST_PAYLOAD = {
    "ok": True,
    "team_id": "T012345",
    "team": "DataTalks",
    "user_id": "U01PAASPXHU",
    "user": "alexey",
}


def fake_transport(calls, status=200, payload=None):
    def transport(method, url, *, headers=None, body=None, timeout=15):
        calls.append({"method": method, "url": url, "headers": headers, "body": body})
        return status, json.dumps(payload if payload is not None else {}).encode()

    return transport


# --- scope policy ------------------------------------------------------------


def test_slack_defaults_to_the_dm_export_user_scopes():
    assert normalize_scopes("slack", []) == sorted(USER_SCOPES)
    assert normalize_scopes("slack", None) == sorted(USER_SCOPES)


def test_slack_keeps_explicit_user_scopes_sorted():
    assert normalize_scopes("slack", ["search:read", "im:history", "im:history"]) == [
        "im:history", "search:read",
    ]


def test_slack_is_an_oauth_consent_provider():
    assert records.uses_oauth_consent({"provider": "slack"}) is True
    assert "slack" not in records.TOKEN_PROVIDERS


# --- authorize URL ------------------------------------------------------------


def test_slack_authorize_url_uses_comma_joined_user_scope():
    url = authorization_url(
        "slack",
        client_id="cid",
        redirect_uri=CALLBACK_URL,
        scopes=USER_SCOPES,
        state="state-token",
        code_challenge="challenge",
    )
    assert url.startswith("https://slack.com/oauth/v2/authorize?")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["user_scope"] == [",".join(USER_SCOPES)]
    assert "scope" not in query
    assert "code_challenge" not in query


# --- code exchange ------------------------------------------------------------


def test_slack_exchange_stores_the_authed_user_token():
    calls = []
    data = exchange_code(
        "slack", code="auth-code", client_id="cid", client_secret="secret",
        redirect_uri=CALLBACK_URL,
        transport=fake_transport(calls, payload=SLACK_TOKEN_PAYLOAD),
    )
    assert data["access_token"] == "xoxp-user-token"
    assert data["scope"].split() == USER_SCOPES
    body = calls[0]["body"].decode()
    assert "code=auth-code" in body
    assert "client_secret=secret" in body
    assert "grant_type" not in body
    assert "code_verifier" not in body


def test_slack_exchange_failure_is_redacted_and_safe():
    calls = []
    with pytest.raises(ProviderError) as exc:
        exchange_code(
            "slack", code="c", client_id="cid", client_secret="super-secret-value",
            redirect_uri=CALLBACK_URL,
            transport=fake_transport(calls, payload={"ok": False, "error": "invalid_code"}),
        )
    assert "super-secret-value" not in str(exc.value)
    assert "xoxp" not in str(exc.value)


def test_slack_exchange_without_a_user_token_fails_closed():
    payload = dict(SLACK_TOKEN_PAYLOAD, authed_user={"id": "U1", "scope": ""})
    with pytest.raises(ProviderError, match="user token"):
        exchange_code(
            "slack", code="c", client_id="cid", client_secret="s",
            redirect_uri=CALLBACK_URL, transport=fake_transport([], payload=payload),
        )


def test_slack_has_no_refresh_grant():
    with pytest.raises(ProviderError, match="reconnect"):
        refresh_access_token(
            "slack", refresh_token="rt", client_id="cid", client_secret="s",
        )


def test_slack_verify_account_binds_the_workspace():
    account_id, title = verify_account(
        "slack", "xoxp-user-token",
        transport=fake_transport([], payload=AUTH_TEST_PAYLOAD),
    )
    assert (account_id, title) == ("T012345", "DataTalks")


def test_slack_revoke_posts_auth_revoke():
    calls = []
    assert revoke_token(
        "slack", "xoxp-user-token", transport=fake_transport(calls, payload={"ok": True}),
    ) is True
    assert calls[0]["url"] == "https://slack.com/api/auth.revoke"
    assert calls[0]["headers"]["authorization"] == "Bearer xoxp-user-token"


# --- connect flow: create, consent URL, callback ------------------------------


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
        from botocore.exceptions import ClientError

        if kwargs.get("ConditionExpression") and kwargs["Item"]["execution_id"] in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[kwargs["Item"]["execution_id"]] = kwargs["Item"]


def _harness(monkeypatch):
    import boto3

    from src.dapier.connections import credentials, oauth_flow
    from src.dapier.auth import session

    connections = ConnectionsTable()
    executions = ExecutionsTable()
    stored = {}

    class Dynamo:
        def Table(self, name):
            return connections if name == "connections" else executions

    def fake_urlopen(request, timeout=15):
        class Response:
            status = 200

            def read(self):
                return json.dumps(SLACK_TOKEN_PAYLOAD).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return Response()

    item = {
        "connection_id": "slack-user",
        "provider": "slack",
        "scopes": list(USER_SCOPES),
        "granted_scopes": [],
        "expected_account_id": None,
        "verified_account_id": None,
        "account_title": None,
        "owner_subject": "subject-1",
        "credential_id": "oauth#slack-user",
        "status": "ready",
        "version": 1,
    }
    connections.items[item["connection_id"]] = item

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("OAUTH_CALLBACK_URL", CALLBACK_URL)
    monkeypatch.setattr("src.dapier.connections.providers.oauth_clients.stored_client",
                        lambda provider: {"client_id": "shared-client-id",
                                          "client_secret": "shared-client-secret"})
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(credentials, "get_credential",
                        lambda credential_id: dict(stored[credential_id]))
    monkeypatch.setattr(credentials, "put_credential",
                        lambda credential_id, value, **kwargs: stored.__setitem__(
                            credential_id, dict(value)))
    monkeypatch.setattr("src.dapier.connections.providers.slack_tokens.verify_account",
                        lambda token, transport=None: ("T012345", "DataTalks"))
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return connections, stored, oauth_flow, session


def _session_cookie(monkeypatch, subject="subject-1"):
    import time as _time

    from src.dapier.auth import session

    return f"dapier_session={session._sign({'sub': 'op@example.test', 'subject': subject, 'exp': int(_time.time()) + 3600})}"


def test_slack_consent_url_carries_user_scope(monkeypatch):
    connections, stored, oauth_flow, session = _harness(monkeypatch)
    event = {"headers": {"host": "fixed.example.test"},
             "cookies": [_session_cookie(monkeypatch)]}

    response = oauth_flow.oauth_start(event, "slack-user")

    assert response["statusCode"] == 302
    location = response["headers"]["location"]
    assert location.startswith("https://slack.com/oauth/v2/authorize?")
    assert "user_scope=" in location
    assert "im%3Ahistory" in location or "im%3ahistory" in location.lower()


def test_slack_callback_stores_the_user_token_without_expiry(monkeypatch):
    connections, stored, oauth_flow, session = _harness(monkeypatch)
    start = oauth_flow.oauth_start(
        {"headers": {"host": "fixed.example.test"},
         "cookies": [_session_cookie(monkeypatch)]}, "slack-user")
    assert start["statusCode"] == 302
    query = urllib.parse.parse_qs(urllib.parse.urlparse(start["headers"]["location"]).query)
    event = {
        "cookies": [start["cookies"][0].split(";", 1)[0], _session_cookie(monkeypatch)],
        "queryStringParameters": {"code": "auth-code", "state": query["state"][0]},
    }

    result = oauth_flow.oauth_callback(event)

    assert result["statusCode"] == 302
    assert result["headers"]["location"] == "/connections?oauth=connected"
    credential = stored["oauth#slack-user"]
    assert credential["access_token"] == "xoxp-user-token"
    assert "xoxb-bot-token" not in json.dumps(credential)
    assert "expires_at" not in credential
    updated = connections.items["slack-user"]
    assert updated["status"] == "connected"
    assert updated["verified_account_id"] == "T012345"
    assert updated["granted_scopes"] == sorted(USER_SCOPES)


# --- issuance: both credential shapes verify ----------------------------------


def _slack_connection(**overrides):
    item = {
        "connection_id": "slack-user",
        "provider": "slack",
        "scopes": list(USER_SCOPES),
        "granted_scopes": list(USER_SCOPES),
        "expected_account_id": None,
        "verified_account_id": "T012345",
        "status": "connected",
        "version": 3,
    }
    item.update(overrides)
    return item


def _store(monkeypatch, value):
    record = {"credential_id": "oauth#slack-user", "value": value, "version": 4}
    monkeypatch.setattr(tokens, "get_credential_record", lambda credential_id: record)


@pytest.mark.parametrize("stored", [
    {"access_token": "xoxp-user-token", "scope": " ".join(USER_SCOPES)},
    {"token": "xoxb-" + "b" * 30, "signing_secret": "shh"},
])
def test_slack_issuance_verifies_either_credential_shape(monkeypatch, stored):
    from src.dapier.connections.providers import slack_tokens

    _store(monkeypatch, dict(stored))
    monkeypatch.setattr(
        slack_tokens, "verify_account", lambda token, transport=None: ("T012345", "DataTalks"))
    token, info = tokens.get_access_token(_slack_connection())
    assert token == (stored.get("access_token") or stored.get("token"))
    assert info["provider_account_id"] == "T012345"
    assert info["expires_at"] is None


def test_slack_issuance_enforces_the_workspace_binding(monkeypatch):
    from src.dapier.connections.providers import slack_tokens
    from src.dapier.connections.records import BindingError

    _store(monkeypatch, {"access_token": "xoxp-user-token"})
    monkeypatch.setattr(
        slack_tokens, "verify_account", lambda token, transport=None: ("TOTHER", "Evil"))
    with pytest.raises(BindingError):
        tokens.get_access_token(_slack_connection())


def test_slack_issuance_without_a_token_fails(monkeypatch):
    _store(monkeypatch, {})
    with pytest.raises(tokens.TokenError):
        tokens.get_access_token(_slack_connection())


# --- legacy paste still imports -------------------------------------------------


def test_slack_import_without_a_token_points_at_oauth(monkeypatch):
    from src.dapier.connections import importing as importing_module

    monkeypatch.setattr(
        importing_module.slack_tokens, "validate_token", lambda token: token)
    status, payload = importing_module.import_core(
        {"connection_id": "slack-user", "provider": "slack"},
        operator_subject="op", connections_table=ConnectionsTable())
    assert status == 400
    assert "OAuth consent" in payload["error"]


# --- CLI ------------------------------------------------------------------------


def test_cli_create_accepts_slack_without_scopes(monkeypatch, capsys):
    from dapier_cli import api, commands

    posted = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        posted.update(method=method, path=path, body=body)
        return {"connection_id": "slack-user"}

    monkeypatch.setattr(api, "call", fake_call)
    assert commands.connections_create(
        "https://api.example.test", "slack-user", "slack", None) == 0
    assert posted["path"] == "/api/agent/connections"
    assert "scopes" not in posted["body"]


def test_cli_parser_lists_slack_as_an_oauth_provider():
    from dapier_cli.main import build_parser

    args = build_parser().parse_args(
        ["connections", "create", "--provider", "slack", "slack-user"])
    assert args.provider == "slack"


def test_console_slack_add_starts_oauth_not_the_token_dialog():
    import json as _json
    from pathlib import Path

    from py_mini_racer import MiniRacer

    root = Path(__file__).resolve().parents[1]
    js_source = (root / "src/web/js/views/connections.js").read_text()
    with MiniRacer() as js:
        js.eval('''
            const nodes = {};
            const $ = id => nodes[id] ||= {innerHTML: '', textContent: '', hidden: false, value: '',
                dataset: {}, setAttribute() {}, addEventListener() {}};
            const $$ = () => [];
            const state = {data: {oauth_clients: []}};
            const escapeHtml = value => String(value);
            const whenMenusClosed = (root, paint) => paint();
            const formatTimestamp = () => '';
            const statusLine = value => value;
            const serviceMark = value => `<i>${value}</i>`;
            const document = {body: {dataset: {}}};
            const connectionsLoadMoreButton = () => ({});
            const bindOAuthLinks = () => {};
            const connectionsPage = {connections: null};
            function allKnownConnections() { return []; }
        ''')
        js.eval(js_source[js_source.index('const CONNECT_SERVICES'):js_source.index('/* Server-paged accounts register')])
        js.eval('''
            const calls = [];
            function openTokenDialog(provider) { calls.push('token:' + provider); }
            function openReuseDialog() { calls.push('reuse'); }
            function startNewOAuthConnection(meta) { calls.push('oauth:' + meta.provider); }
        ''')
        js.eval(js_source[js_source.index('function reusableGoogleConnections('):js_source.index('function openReuseDialog(')])
        js.eval('connectService("slack"); connectService("telegram");')
        assert _json.loads(js.eval('JSON.stringify(calls)')) == ['oauth:slack', 'token:telegram']
