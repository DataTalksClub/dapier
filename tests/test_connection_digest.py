"""Tests for the scheduled connection-expiry digest
(src/dapier/connection_digest.py).

The digest reads the same connections register the console and CLI read
(records.api_list_connections rendered through public_view, expiry from the
credentials store) and emails the operator every connection whose token
expires within the window or already has; zero expiring connections skip
the send. Send-now parity lives on POST
/api/{admin,agent}/connections/expiry-digest and
``dapier connections send-expiry-digest``.
"""

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from src.dapier import connection_digest

# Wall-clock-relative instants: health() compares the stored expiry against
# the real clock, so the fixtures must too, whatever day the suite runs.
REAL_NOW = datetime.now(timezone.utc)


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


@contextmanager
def _ses_client():
    """A stubbed SES client whose send_email returns a message id."""
    with patch("boto3.client") as mocked:
        mocked.return_value.send_email.return_value = {"MessageId": "m1"}
        yield mocked


@pytest.fixture
def ses():
    with _ses_client() as mocked:
        yield mocked


def _connection(connection_id, provider="google", status="connected"):
    return {"connection_id": connection_id, "provider": provider,
            "display_name": connection_id, "status": status, "scopes": [],
            "version": 1}


class FakeTable:
    """The scan surface api_list_connections reads."""

    def __init__(self, items):
        self._items = items

    def scan(self, **kwargs):
        return {"Items": self._items}


def _expiring_store(monkeypatch, stored_by_id):
    """Point the credentials store at fixture values (epoch expiries)."""
    from src.dapier.connections import credentials as connection_credentials

    monkeypatch.setattr(
        connection_credentials, "get_credential_record",
        lambda credential_id: {
            "value": stored_by_id.get(credential_id.split("#", 1)[1], {})})


def _expiring_rows():
    return [
        {"connection_id": "google-sheets", "provider": "google",
         "display_name": "alexey@datatalks.club",
         "token_expires_at": (REAL_NOW + timedelta(hours=6)).isoformat(),
         "expires_state": "expiring"},
        {"connection_id": "dropbox", "provider": "dropbox", "display_name": "dropbox",
         "token_expires_at": (REAL_NOW - timedelta(hours=2)).isoformat(),
         "expires_state": "expired"},
    ]


# --- expiring: window classification ---

def test_expiring_flags_within_window_and_expired(monkeypatch):
    stored = {
        "soon": {"expires_at": int((REAL_NOW + timedelta(hours=12)).timestamp())},
        "far": {"expires_at": int((REAL_NOW + timedelta(days=30)).timestamp())},
        "past": {"expires_at": int((REAL_NOW - timedelta(hours=2)).timestamp())},
    }
    _expiring_store(monkeypatch, stored)
    items = [_connection("soon"), _connection("far"), _connection("past"),
             _connection("bot-token", provider="slack")]

    rows = connection_digest.expiring(48, now=REAL_NOW, table=FakeTable(items))

    states = {row["connection_id"]: row["expires_state"] for row in rows}
    assert states == {"soon": "expiring", "past": "expired"}


def test_expiring_includes_revoked_without_stored_expiry(monkeypatch):
    _expiring_store(monkeypatch, {})
    items = [_connection("gone", provider="zoom", status="revoked")]

    rows = connection_digest.expiring(48, now=REAL_NOW, table=FakeTable(items))

    assert [(row["connection_id"], row["expires_state"]) for row in rows] == [
        ("gone", "expired")]


def test_expiring_sorts_soonest_first(monkeypatch):
    stored = {
        "a-later": {"expires_at": int((REAL_NOW + timedelta(hours=40)).timestamp())},
        "b-sooner": {"expires_at": int((REAL_NOW + timedelta(hours=4)).timestamp())},
    }
    _expiring_store(monkeypatch, stored)
    items = [_connection("a-later"), _connection("b-sooner")]

    rows = connection_digest.expiring(48, now=REAL_NOW, table=FakeTable(items))

    assert [row["connection_id"] for row in rows] == ["b-sooner", "a-later"]


# --- render: subject counts, body names the connections and the fix ---

def test_render_names_connections_and_the_reauth_command():
    subject, body = connection_digest.render(_expiring_rows(), 48)

    assert subject == ("dapier connection digest: 2 connection token(s) "
                       "need re-authentication")
    assert "google-sheets" in body and "dropbox" in body
    assert "dapier connections connect google-sheets" in body
    assert "EXPIRED" in body
    assert "(1 already expired)" in body


def test_render_singular_case_has_no_expired_caveat():
    subject, body = connection_digest.render([_expiring_rows()[0]], 48)

    assert subject == ("dapier connection digest: 1 connection token(s) "
                       "need re-authentication")
    assert "already expired" not in body


# --- send / handler: the daily schedule's render-and-send ---

def test_handler_emails_the_expiring_connections(monkeypatch, ses):
    rows = _expiring_rows()
    monkeypatch.setattr(connection_digest, "expiring", lambda *a, **k: rows)
    monkeypatch.delenv("DAPIER_CONNECTION_DIGEST_HOURS", raising=False)
    monkeypatch.setenv("DAPIER_NOTIFY_EMAIL", "ops@example.test")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dtcdev.click")

    result = connection_digest.handler({})

    assert result["sent"] is True
    assert result["to"] == "ops@example.test"
    assert result["connections"] == ["google-sheets", "dropbox"]
    kwargs = ses.return_value.send_email.call_args.kwargs
    assert kwargs["Source"] == "no-reply@dtcdev.click"
    assert kwargs["Destination"]["ToAddresses"] == ["ops@example.test"]
    assert "2 connection token(s)" in kwargs["Message"]["Subject"]["Data"]
    assert "google-sheets" in kwargs["Message"]["Body"]["Text"]["Data"]


def test_send_with_nothing_expiring_skips(monkeypatch, ses):
    monkeypatch.setattr(connection_digest, "expiring", lambda *a, **k: [])
    monkeypatch.delenv("DAPIER_CONNECTION_DIGEST_HOURS", raising=False)
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@dtcdev.click")

    result = connection_digest.handler({})

    assert result == {"skipped": True, "window_hours": 48, "expiring": 0}
    ses.return_value.send_email.assert_not_called()


# --- POST /api/admin/connections/expiry-digest (console path) ---

def _admin_request(method, path, cookies=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
        "body": "{}",
    }


def _admin_operator(monkeypatch):
    from src.dapier.auth import session

    monkeypatch.setattr(session, "_credentials",
                        lambda: {"username": "admin", "password": "pw"})
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    return [f"dapier_session={cookie}"]


def test_admin_expiry_digest_send_now_returns_what_was_sent(monkeypatch, ses):
    from src.dapier.api import admin

    cookies = _admin_operator(monkeypatch)
    monkeypatch.setattr(connection_digest, "expiring",
                        lambda *a, **k: _expiring_rows())
    monkeypatch.setenv("DAPIER_NOTIFY_EMAIL", "ops@dtcdev.click")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@dtcdev.click")
    response = admin.route(
        _admin_request("POST", "/api/admin/connections/expiry-digest",
                       cookies=cookies),
        "POST", "/api/admin/connections/expiry-digest",
    )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["sent"] is True
    assert body["to"] == "ops@dtcdev.click"
    assert body["connections"] == ["google-sheets", "dropbox"]
    ses.return_value.send_email.assert_called_once()


def test_admin_expiry_digest_requires_authentication(monkeypatch):
    from src.dapier.api import admin

    response = admin.route(
        _admin_request("POST", "/api/admin/connections/expiry-digest"),
        "POST", "/api/admin/connections/expiry-digest",
    )

    assert response["statusCode"] == 401


# --- POST /api/agent/connections/expiry-digest (CLI path, operator-gated) ---

def _agent_operator(monkeypatch):
    from src.dapier.api import agent as agent_api

    agent_api.reset_rate_limits()

    class Table:
        def get_item(self, **kwargs):
            return {}

        def put_item(self, **kwargs):
            return {}

        def scan(self, **kwargs):
            return {"Items": []}

    class DynamoResource:
        def Table(self, _name):
            return Table()

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: DynamoResource())
    return agent_api


def _agent_event(token, body=None):
    headers = {"host": "dapier.example.test"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = {"headers": headers, "cookies": []}
    if body is not None:
        request["body"] = json.dumps(body)
    return request


def test_agent_expiry_digest_send_now_returns_what_was_sent(monkeypatch, ses):
    agent_api = _agent_operator(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setattr(connection_digest, "expiring",
                        lambda *a, **k: _expiring_rows())
    monkeypatch.setenv("DAPIER_NOTIFY_EMAIL", "ops@dtcdev.click")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@dtcdev.click")
    response = agent_api.route(
        _agent_event("dtc-id-token"), "POST",
        "/api/agent/connections/expiry-digest")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["sent"] is True
    assert body["connections"] == ["google-sheets", "dropbox"]
    ses.return_value.send_email.assert_called_once()


def test_agent_expiry_digest_requires_operator(monkeypatch):
    agent_api = _agent_operator(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "subject-1", "email": "agent@example.test"})

    response = agent_api.route(
        _agent_event("dtc-id-token"), "POST",
        "/api/agent/connections/expiry-digest")

    assert response["statusCode"] == 403


# --- `dapier connections send-expiry-digest` (thin client over the agent route) ---

def test_cli_send_expiry_digest_hits_the_agent_endpoint(isolated_home, monkeypatch, capsys):
    from dapier_cli import commands, main

    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"sent": True, "to": "ops@dtcdev.click", "subject": "x",
                "connections": ["google-sheets"], "window_hours": 48}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["connections", "send-expiry-digest"])

    assert rc == 0
    assert calls == [("POST", "/api/agent/connections/expiry-digest")]
    out = capsys.readouterr().out
    assert "Digest sent to ops@dtcdev.click" in out
    assert "google-sheets" in out


def test_cli_send_expiry_digest_reports_the_skip(isolated_home, monkeypatch, capsys):
    from dapier_cli import commands, main

    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"skipped": True, "window_hours": 48, "expiring": 0}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["connections", "send-expiry-digest"])

    assert rc == 0
    assert "skipped" in capsys.readouterr().out


# --- deploy wiring: the daily EventBridge schedule targets the handler ---

def test_template_schedules_the_connection_digest_daily():
    text = open("template.yaml", encoding="utf-8").read()
    assert "ConnectionDigestFunction:" in text
    assert "src.dapier.connection_digest.handler" in text
    assert "DAPIER_CONNECTION_DIGEST_HOURS: '48'" in text
    assert "CONNECTIONS_TABLE: !Ref ConnectionsTable" in text
    assert "CREDENTIALS_TABLE: !Ref CredentialsTable" in text
    assert "DAPIER_NOTIFY_EMAIL: !Ref NotifyEmail" in text
    assert "Schedule: cron(30 7 * * ? *)" in text
    assert "ConnectionDigestErrorAlarm:" in text
