"""Connection listing health: offline ``health`` + ``token_expires_at``.

The stored OAuth token lives in the credentials store; listings must flag
connections whose token is past its expiry ("needs reconnection") without
ever leaking the secret itself. Covers the domain view, both API surfaces
(`/api/agent/connections`, `/api/admin/overview`), and the CLI output.
"""

import json
import time

import pytest

from src.dapier.api import overview as overview_api
from src.dapier.api import agent as agent_api
from src.dapier.connections.records import public_view


# ---------------------------------------------------------------- domain view

def _item(**overrides):
    item = {
        "connection_id": "youtube-personal",
        "provider": "youtube",
        "display_name": "Personal YouTube",
        "scopes": ["s"],
        "granted_scopes": ["s"],
        "status": "connected",
        "verified_account_id": "UC1",
        "account_title": "Ch",
        "version": 3,
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    item.update(overrides)
    return item


def test_future_expiry_is_ok_and_exposed():
    stored = {"access_token": "at", "refresh_token": "rt",
              "expires_at": int(time.time()) + 3600}
    view = public_view(_item(), stored)
    assert view["health"] == "ok"
    assert view["token_expires_at"]
    assert view["status"] == "connected"


def test_past_expiry_is_expired():
    stored = {"access_token": "at", "refresh_token": "rt", "expires_at": 1_000}
    view = public_view(_item(), stored)
    assert view["health"] == "expired"
    assert view["token_expires_at"]


def test_expiry_within_the_refresh_skew_already_reads_expired():
    # health shares the token-refresh skew, so a listing never calls a token
    # "ok" that get_access_token would immediately refresh.
    borderline = {"expires_at": int(time.time()) + 60}
    assert public_view(_item(), borderline)["health"] == "expired"


def test_no_expiry_is_ok_without_token_expires_at():
    for stored in (None, {}, {"access_token": "at"}):
        view = public_view(_item(), stored)
        assert view["health"] == "ok"
        assert view["token_expires_at"] is None


def test_awaiting_consent_stays_ok():
    view = public_view(_item(status="ready"), None)
    assert view["health"] == "ok"
    assert view["status"] == "ready"


def test_revoked_is_expired_even_with_a_live_token():
    stored = {"access_token": "at", "expires_at": int(time.time()) + 3600}
    view = public_view(_item(status="revoked"), stored)
    assert view["health"] == "expired"
    assert view["status"] == "revoked"


def test_view_never_leaks_the_stored_secret():
    stored = {"access_token": "secret-access", "refresh_token": "secret-refresh",
              "client_secret": "secret-client", "expires_at": 1_000}
    dumped = json.dumps(public_view(_item(), stored))
    for secret in ("secret-access", "secret-refresh", "secret-client",
                   "access_token", "refresh_token", "client_secret"):
        assert secret not in dumped


# ------------------------------------------------------------- agent surface

class Table:
    def __init__(self, items=None):
        self.items = items or {}

    def _lookup(self, key):
        if "grantee" in key:
            return self.items.get((key.get("connection_id"), key.get("grantee")))
        return self.items.get(key.get("connection_id"), self.items.get(key.get("credential_id")))

    def get_item(self, **kwargs):
        item = self._lookup(kwargs["Key"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        if "grantee" in item:
            self.items[(item["connection_id"], item["grantee"])] = item
        else:
            self.items[item.get("connection_id") or item.get("credential_id")] = item

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def configure(monkeypatch, *, claims, connections, grants):
    agent_api.reset_rate_limits()
    tables = {
        "connections": Table(connections),
        "grants": Table(grants),
        "credentials": Table(),
        "api-tokens": Table(),
    }

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: dict(claims),
    )
    return tables


def event(query=None):
    request = {"headers": {"host": "dapier.example.test", "authorization": "Bearer t"},
               "cookies": []}
    if query is not None:
        request["queryStringParameters"] = query
    return request


GRANT = {"connection_id": "youtube-personal", "grantee": "subject-1#uploader",
         "subject": "subject-1", "agent": "uploader", "operations": ["use"]}
NO_EXPIRY_GRANT = {"connection_id": "slack-team", "grantee": "subject-1#poster",
                   "subject": "subject-1", "agent": "poster", "operations": ["use"]}


def test_agent_list_and_show_carry_health(monkeypatch):
    tables = configure(
        monkeypatch, claims={"sub": "subject-1"},
        connections={
            "youtube-personal": _item(),
            "slack-team": _item(connection_id="slack-team", provider="slack",
                                display_name="Team Slack", verified_account_id=None,
                                account_title=None),
        },
        grants={("youtube-personal", "subject-1#uploader"): GRANT,
                ("slack-team", "subject-1#poster"): NO_EXPIRY_GRANT},
    )
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    tables["credentials"].put_item(Item={
        "credential_id": "oauth#youtube-personal", "provider": "google", "version": 1,
        "value": {"access_token": "at", "refresh_token": "rt", "expires_at": 1_000},
    })

    listed = json.loads(agent_api.route(event(), "GET", "/api/agent/connections")["body"])
    by_id = {row["connection_id"]: row for row in listed["connections"]}
    assert by_id["youtube-personal"]["health"] == "expired"
    assert by_id["youtube-personal"]["token_expires_at"]
    # No expiring token (pasted bot token, nothing stored yet) stays ok.
    assert by_id["slack-team"]["health"] == "ok"
    assert by_id["slack-team"]["token_expires_at"] is None

    shown = json.loads(
        agent_api.route(event(query={"agent": "uploader"}),
                        "GET", "/api/agent/connections/youtube-personal")["body"])
    assert shown["health"] == "expired"
    assert shown["token_expires_at"]

    # A provider-side refresh (new stored expiry) flips the listing back to ok.
    tables["credentials"].put_item(Item={
        "credential_id": "oauth#youtube-personal", "provider": "google", "version": 2,
        "value": {"access_token": "at2", "refresh_token": "rt",
                  "expires_at": int(time.time()) + 3600},
    })
    fresh = json.loads(agent_api.route(event(), "GET", "/api/agent/connections")["body"])
    by_id = {row["connection_id"]: row for row in fresh["connections"]}
    assert by_id["youtube-personal"]["health"] == "ok"


def test_agent_revoked_connection_needs_reconnection(monkeypatch):
    tables = configure(
        monkeypatch, claims={"sub": "subject-1"},
        connections={"youtube-personal": _item(status="revoked")},
        grants={("youtube-personal", "subject-1#uploader"): GRANT},
    )
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    # Revocation clears the stored tokens; the status alone drives the verdict.
    listed = json.loads(agent_api.route(event(), "GET", "/api/agent/connections")["body"])
    row = listed["connections"][0]
    assert row["status"] == "revoked"
    assert row["health"] == "expired"
    assert row["token_expires_at"] is None


# ------------------------------------------------------------- admin surface

def _overview(monkeypatch, connections, credential_items):
    import boto3

    creds = Table(credential_items)

    class Dynamo:
        def Table(self, name):
            return creds

    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.delenv("TASK_USAGE_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(overview_api, "_workflows", lambda *args, **kwargs: [])
    monkeypatch.setattr(overview_api, "_scan",
                        lambda *args, **kwargs: connections if args and args[0] == "connections" else [])
    monkeypatch.setattr(overview_api.runs, "recent", lambda *args, **kwargs: [])
    monkeypatch.setattr(overview_api, "_credential_status", lambda provider: {"provider": provider})
    monkeypatch.setattr(overview_api, "_oauth_client_status", lambda provider: {"provider": provider})
    monkeypatch.setattr(overview_api.api_tokens, "list_all", lambda: [])
    monkeypatch.setattr(overview_api, "_email_triggers",
                        lambda: {"domain": "", "triggers": [], "managed_routes": []})
    request = {"requestContext": {"http": {"method": "GET", "path": "/api/admin/overview"}},
               "headers": {"host": "dapier.example.test"}, "cookies": []}
    return json.loads(overview_api.overview(request)["body"])["connections"]


def test_overview_connections_carry_health_and_expiry(monkeypatch):
    expired_stored = {"credential_id": "oauth#youtube-personal", "provider": "google",
                      "version": 1,
                      "value": {"access_token": "at", "refresh_token": "rt",
                                "expires_at": 1_000}}
    fresh_stored = {"credential_id": "oauth#dropbox-work", "provider": "dropbox",
                    "version": 1,
                    "value": {"access_token": "at", "refresh_token": "rt",
                              "expires_at": int(time.time()) + 3600}}
    connections = [
        _item(),
        _item(connection_id="dropbox-work", provider="dropbox",
              display_name="Work Dropbox"),
        _item(connection_id="slack-team", provider="slack", display_name="Team Slack",
              status="revoked"),
    ]
    stored = {item["credential_id"]: item for item in (expired_stored, fresh_stored)}
    rows = {row["connection_id"]: row
            for row in _overview(monkeypatch, connections, stored)}
    assert rows["youtube-personal"]["health"] == "expired"
    assert rows["youtube-personal"]["token_expires_at"]
    assert rows["dropbox-work"]["health"] == "ok"
    assert rows["dropbox-work"]["token_expires_at"]
    assert rows["slack-team"]["health"] == "expired"
    assert rows["slack-team"]["token_expires_at"] is None
    assert "refresh_token" not in json.dumps(rows)


# ----------------------------------------------------------------- CLI output

def test_cli_connections_list_shows_health_and_expiry(monkeypatch, capsys):
    from dapier_cli import commands

    def fake_call(url, method, path, body=None, debug=False):
        assert path == "/api/agent/connections"
        return {"connections": [
            {"connection_id": "youtube-personal", "provider": "youtube",
             "status": "connected", "health": "expired",
             "token_expires_at": "2026-06-15T12:00:00+00:00", "account_title": "Ch"},
            {"connection_id": "slack-team", "provider": "slack",
             "status": "connected", "health": "ok"},
        ]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_list("https://api.example.test") == 0
    out = capsys.readouterr().out
    assert "HEALTH" in out and "EXPIRES" in out
    assert "expired" in out
    assert "2026-06-15" in out
    assert " ok " in out


def test_cli_connections_show_prints_health(monkeypatch, capsys):
    from dapier_cli import commands

    def fake_call(url, method, path, body=None, debug=False):
        assert path == "/api/agent/connections/youtube-personal"
        return {"connection_id": "youtube-personal", "provider": "youtube",
                "status": "connected", "health": "expired",
                "token_expires_at": "2026-06-15T12:00:00+00:00"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_show("https://api.example.test", "youtube-personal") == 0
    out = capsys.readouterr().out
    assert "health: expired" in out
    assert "token_expires_at: 2026-06-15" in out
