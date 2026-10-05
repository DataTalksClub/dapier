"""Tests for the scheduled operator error digest (src/dapier/error_digest.py).

The digest renders the same errors summary the console and CLI read
(api/errors.py api_summary over runs.recent) into one SES email to the
operator address; zero failures skip the send. Send-now parity lives on
POST /api/{admin,agent}/errors/digest and `dapier errors send-digest`.
"""

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.dapier import error_digest


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def _failed_runs():
    return [
        {"run_id": "wf-1:e2", "workflow_id": "wf-1", "status": "error",
         "started_at": "2026-09-26T10:00:00+00:00", "error": "connection timeout"},
        {"run_id": "wf-1:e1", "workflow_id": "wf-1", "status": "failed",
         "started_at": "2026-09-25T10:00:00+00:00", "error": "boom"},
        {"run_id": "wf-2:e3", "workflow_id": "wf-2", "status": "failed",
         "started_at": "2026-09-26T11:00:00+00:00", "error": "Slack rejected message"},
    ]


def _stub_summary(monkeypatch, runs):
    """Point api_summary's window scan at fixture rows (no DynamoDB)."""
    from src.dapier.api import runs as runs_api

    problems = [run for run in runs if run.get("status") in ("failed", "error")]
    monkeypatch.setattr(runs_api, "recent", lambda *a, **k: problems)
    monkeypatch.delenv("DAPIER_ERROR_DIGEST_DAYS", raising=False)


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


# --- render: subject counts, body lists the failing workflows ---

def test_render_counts_workflows_and_names_the_last_failure():
    summary = {"window_days": 1, "total_failed_runs": 3, "workflows": [
        {"workflow_id": "wf-1", "failed_runs": 2,
         "last_failed_at": "2026-09-26T10:00:00+00:00",
         "last_error": "connection timeout"},
        {"workflow_id": "wf-2", "failed_runs": 1,
         "last_failed_at": "2026-09-26T11:00:00+00:00",
         "last_error": "Slack rejected message"},
    ]}

    subject, body = error_digest.render(summary)

    assert subject == "dapier error digest: 3 failed runs (last 24h)"
    assert "wf-1" in body and "wf-2" in body
    assert "2 failed" in body and "1 failed" in body
    assert "connection timeout" in body


def test_render_singular_run_and_multi_day_window():
    subject, _ = error_digest.render({
        "window_days": 7, "total_failed_runs": 1,
        "workflows": [{"workflow_id": "wf-1", "failed_runs": 1,
                       "last_failed_at": "2026-09-25T10:00:00+00:00",
                       "last_error": "boom"}],
    })

    assert subject == "dapier error digest: 1 failed run (last 7 days)"


# --- bounded scan: the summary flags a hit cap and the digest says so ---

def _failed_run(index):
    return {"run_id": f"wf-1:e{index}", "workflow_id": "wf-1", "status": "failed",
            "started_at": f"2026-09-26T10:00:{index:02d}+00:00", "error": "boom"}


def test_api_summary_flags_when_the_scan_cap_was_hit(monkeypatch):
    from src.dapier.api import errors as errors_api
    from src.dapier.api import runs as runs_api

    monkeypatch.setattr(runs_api, "MAX_LIMIT", 3)
    monkeypatch.setattr(runs_api, "recent", lambda *a, **k: [_failed_run(i) for i in range(3)])

    _, payload = errors_api.api_summary(1, now=datetime(2026, 9, 27, tzinfo=timezone.utc))

    assert payload["total_failed_runs"] == 3
    assert payload["bounded"] is True
    assert payload["cap"] == 3


def test_api_summary_below_the_cap_stays_unqualified(monkeypatch):
    from src.dapier.api import errors as errors_api
    from src.dapier.api import runs as runs_api

    monkeypatch.setattr(runs_api, "MAX_LIMIT", 3)
    monkeypatch.setattr(runs_api, "recent", lambda *a, **k: [_failed_run(i) for i in range(2)])

    _, payload = errors_api.api_summary(1, now=datetime(2026, 9, 27, tzinfo=timezone.utc))

    assert payload["total_failed_runs"] == 2
    assert "bounded" not in payload
    assert "cap" not in payload


def test_render_says_when_only_the_cap_was_counted():
    summary = {"window_days": 1, "total_failed_runs": 200, "bounded": True, "cap": 200,
               "workflows": [{"workflow_id": "wf-1", "failed_runs": 200,
                              "last_failed_at": "2026-09-26T10:00:00+00:00",
                              "last_error": "boom"}]}

    _subject, body = error_digest.render(summary)

    assert "only the most recent 200 failed runs are counted" in body
    assert "wf-1" in body


def test_render_without_a_cap_adds_no_caveat():
    summary = {"window_days": 1, "total_failed_runs": 2,
               "workflows": [{"workflow_id": "wf-1", "failed_runs": 2,
                              "last_failed_at": "2026-09-26T10:00:00+00:00",
                              "last_error": "boom"}]}

    _subject, body = error_digest.render(summary)

    assert "only the most recent" not in body


# --- handler: the daily schedule's render-and-send ---

def test_handler_renders_the_summary_into_the_ses_payload(monkeypatch, ses):
    _stub_summary(monkeypatch, _failed_runs())
    with patch.dict(os.environ, {"DAPIER_EMAIL_SENDER": "ops@dtcdev.click"}, clear=False):
        monkeypatch.delenv("DAPIER_NOTIFY_EMAIL", raising=False)
        monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)
        result = error_digest.handler({})

    assert result["sent"] is True
    assert result["message_id"] == "m1"
    kwargs = ses.return_value.send_email.call_args.kwargs
    assert kwargs["Source"] == "ops@dtcdev.click"
    assert kwargs["Destination"]["ToAddresses"] == ["ops@dtcdev.click"]
    assert "3 failed runs" in kwargs["Message"]["Subject"]["Data"]
    body = kwargs["Message"]["Body"]["Text"]["Data"]
    assert "wf-1" in body and "wf-2" in body
    assert "connection timeout" in body


def test_handler_sends_to_the_notify_inbox_from_the_sender(monkeypatch, ses):
    _stub_summary(monkeypatch, _failed_runs())
    monkeypatch.setenv("DAPIER_NOTIFY_EMAIL", "ops@example.test")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dtcdev.click")

    result = error_digest.handler({})

    assert result["to"] == "ops@example.test"
    kwargs = ses.return_value.send_email.call_args.kwargs
    assert kwargs["Source"] == "no-reply@dtcdev.click"
    assert kwargs["Destination"]["ToAddresses"] == ["ops@example.test"]


def test_handler_with_zero_failures_skips_the_send(monkeypatch, ses):
    _stub_summary(monkeypatch, [])
    with patch.dict(os.environ, {"DAPIER_EMAIL_SENDER": "ops@dtcdev.click"}):
        result = error_digest.handler({})

    assert result == {"skipped": True, "window_days": 1, "total_failed_runs": 0}
    ses.return_value.send_email.assert_not_called()


# --- POST /api/admin/errors/digest (console path, session-gated) ---

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


def test_admin_digest_send_now_returns_what_was_sent(monkeypatch, ses):
    from src.dapier.api import admin

    cookies = _admin_operator(monkeypatch)
    _stub_summary(monkeypatch, _failed_runs())
    monkeypatch.delenv("DAPIER_NOTIFY_EMAIL", raising=False)
    monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)
    with patch.dict(os.environ, {"DAPIER_EMAIL_SENDER": "ops@dtcdev.click"}):
        response = admin.route(
            _admin_request("POST", "/api/admin/errors/digest", cookies=cookies),
            "POST", "/api/admin/errors/digest",
        )

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["sent"] is True
    assert body["to"] == "ops@dtcdev.click"
    assert "3 failed runs" in body["subject"]
    assert "wf-1" in body["body"]
    ses.return_value.send_email.assert_called_once()


def test_admin_digest_requires_authentication(monkeypatch):
    from src.dapier.api import admin

    response = admin.route(_admin_request("POST", "/api/admin/errors/digest"),
                           "POST", "/api/admin/errors/digest")

    assert response["statusCode"] == 401


# --- POST /api/agent/errors/digest (CLI path, operator-gated) ---

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


def test_agent_digest_send_now_returns_what_was_sent(monkeypatch, ses):
    agent_api = _agent_operator(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "op-1", "email": "op@datatalks.club"})
    _stub_summary(monkeypatch, _failed_runs())
    monkeypatch.delenv("DAPIER_NOTIFY_EMAIL", raising=False)
    monkeypatch.delenv("BACKUP_ALERT_EMAIL", raising=False)
    with patch.dict(os.environ, {"DAPIER_EMAIL_SENDER": "ops@dtcdev.click"}):
        response = agent_api.route(
            _agent_event("dtc-id-token"), "POST", "/api/agent/errors/digest")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["sent"] is True
    assert body["to"] == "ops@dtcdev.click"
    assert "3 failed runs" in body["subject"]
    ses.return_value.send_email.assert_called_once()


def test_agent_digest_requires_operator(monkeypatch):
    agent_api = _agent_operator(monkeypatch)
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "subject-1", "email": "agent@example.test"})

    response = agent_api.route(
        _agent_event("dtc-id-token"), "POST", "/api/agent/errors/digest")

    assert response["statusCode"] == 403


# --- `dapier errors send-digest` (thin client over the agent route) ---

def test_cli_send_digest_hits_the_agent_endpoint(isolated_home, monkeypatch, capsys):
    from dapier_cli import commands, main

    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"sent": True, "to": "ops@dtcdev.click", "subject": "x", "total_failed_runs": 2,
                "window_days": 1}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["errors", "send-digest"])

    assert rc == 0
    assert calls == [("POST", "/api/agent/errors/digest")]
    out = capsys.readouterr().out
    assert "Digest sent to ops@dtcdev.click" in out and "2 failed runs" in out


def test_cli_send_digest_reports_the_skip(isolated_home, monkeypatch, capsys):
    from dapier_cli import commands, main

    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"skipped": True, "total_failed_runs": 0, "window_days": 1}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["errors", "send-digest"])

    assert rc == 0
    assert "skipped" in capsys.readouterr().out


# --- deploy wiring: the daily EventBridge schedule targets the handler ---

def test_template_schedules_the_digest_daily():
    text = open("template.yaml", encoding="utf-8").read()
    assert "ErrorDigestFunction:" in text
    assert "src.dapier.error_digest.handler" in text
    assert "DAPIER_EMAIL_SENDER: !Ref EmailSender" in text
    assert "DAPIER_NOTIFY_EMAIL: !Ref NotifyEmail" in text
    assert "ses:SendEmail" in text
    assert "Schedule: cron(0 7 * * ? *)" in text
