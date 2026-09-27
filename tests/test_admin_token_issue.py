"""Console fresh-token route: operator parity for the CLI's token exec/write."""

import json
import time

import boto3

from src.dapier.api import admin
from src.dapier.auth import session


def _operator_cookies(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    cookie = session._sign({
        "sub": "op@datatalks.club", "subject": "op-1", "exp": int(time.time()) + 600,
    })
    return [f"dapier_session={cookie}"]


def _route(monkeypatch, connection):
    class Table:
        def get_item(self, **kwargs):
            return {"Item": connection}

    class Dynamo:
        def Table(self, _name):
            return Table()

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")


def test_issue_connection_token_returns_short_lived_token(monkeypatch):
    from src.dapier.connections import tokens as connection_tokens

    _route(monkeypatch, {"connection_id": "yt-1", "provider": "youtube",
                         "credential_id": "oauth#yt-1", "status": "connected"})
    cookies = _operator_cookies(monkeypatch)
    monkeypatch.setattr(connection_tokens, "get_access_token", lambda item: (
        "ya29.fresh",
        {"expires_at": "2026-09-28T13:00:00Z", "scope": "sheets.read",
         "provider_account_id": "acct-1", "account_title": "me", "refreshed": False},
    ))

    path = "/api/admin/connections/yt-1/token"
    response = admin.route(_request("POST", path, cookies=cookies), "POST", path)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["access_token"] == "ya29.fresh"
    assert body["provider"] == "youtube"
    assert body["provider_account_id"] == "acct-1"
    assert response["headers"]["cache-control"] == "no-store"


def test_issue_connection_token_unknown_connection_is_404(monkeypatch):
    _route(monkeypatch, None)
    cookies = _operator_cookies(monkeypatch)

    path = "/api/admin/connections/nope/token"
    response = admin.route(_request("POST", path, cookies=cookies), "POST", path)

    assert response["statusCode"] == 404


def test_issue_connection_token_provider_outage_is_502(monkeypatch):
    from src.dapier.connections.tokens import TokenError
    from src.dapier.connections import tokens as connection_tokens

    _route(monkeypatch, {"connection_id": "yt-1", "provider": "youtube",
                         "credential_id": "oauth#yt-1", "status": "connected"})
    cookies = _operator_cookies(monkeypatch)
    def _boom(item):
        raise TokenError("refresh failed")
    monkeypatch.setattr(connection_tokens, "get_access_token", _boom)

    path = "/api/admin/connections/yt-1/token"
    response = admin.route(_request("POST", path, cookies=cookies), "POST", path)

    assert response["statusCode"] == 502
    assert "unavailable" in json.loads(response["body"])["error"]


def _request(method, path, body=None, cookies=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
        "body": json.dumps(body) if body is not None else None,
    }
