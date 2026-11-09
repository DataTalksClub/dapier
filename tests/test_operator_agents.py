"""Orin holds the operator role, and the owner filter for everyone else.

``orin@datatalks.club`` resolves through the operator allowlist exactly
like Alexey's account (the directory row keeps the ``orin`` alias for
``--owner orin`` and the console "View as" control). The ``agent`` band
(``AGENT_ACTIONS`` + ``console_allowed`` + owner-scoped visibility) stays
in place for future least-privilege rows."""

import argparse
import json
import time
from pathlib import Path

import pytest

from src.dapier.api import runs
from src.dapier.api.admin import dispatch
from src.dapier.auth import actors, session, visibility

ROOT = Path(__file__).resolve().parents[1]


def _token(monkeypatch, sub, subject):
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    return session._sign({"sub": sub, "subject": subject, "exp": int(time.time()) + 3600})


def test_orin_is_a_full_operator_like_alexey():
    payload = {"sub": "orin@datatalks.club", "subject": "sub-orin"}
    assert not actors.is_agent(payload)
    assert actors.gate(payload) == "operator"
    assert actors.stable_subject(payload) == "sub-orin"
    assert actors.action_allowed(payload, "runs")
    assert actors.action_allowed(payload, "workflow.publish")
    assert actors.action_allowed(payload, "typefully.social_sets")
    assert actors.action_allowed(payload, "typefully.upload_media")
    assert actors.action_allowed(payload, "typefully.media_status")
    assert actors.action_allowed(payload, "typefully.patch_draft")
    assert actors.action_allowed(payload, "typefully.schedule_draft")
    # Operator-only actions pass for Orin exactly as for Alexey.
    assert actors.action_allowed(payload, "triggers.inbox")
    assert actors.action_allowed(payload, "schedule-trigger")
    assert actors.action_allowed(payload, "poll-trigger")
    assert actors.action_allowed(payload, "errors")
    assert actors.action_allowed(payload, "credential")
    assert actors.action_allowed(payload, "quota.set")
    assert actors.console_allowed("GET", "/api/admin/runs")
    assert actors.console_allowed("PUT", "/api/admin/designer/workflows")
    assert actors.console_allowed("GET", "/api/admin/triggers/inbox")
    # The agent band stays closed on these paths for agent rows; Orin no
    # longer goes through it — dispatch only consults console_allowed for
    # agent payloads (is_agent is False for Orin now), so the full console
    # routes (schedules, polls, errors, credentials, tokens) reach her.
    assert not actors.console_allowed("GET", "/api/admin/schedule-triggers")
    assert not actors.console_allowed("GET", "/api/admin/poll-triggers")
    assert not actors.console_allowed("GET", "/api/admin/errors/summary")
    assert not actors.console_allowed("PUT", "/api/admin/credentials/slack")
    assert not actors.console_allowed("DELETE", "/api/admin/tokens")
    scope = visibility.for_session(payload)
    assert scope.is_operator is True
    assert scope.subject == "sub-orin"
    alexey = {"sub": "alexey@datatalks.club", "subject": "sub-alex"}
    assert visibility.for_session(alexey).is_operator is True
    assert actors.gate(alexey) == "operator"


def test_a_full_operator_is_not_narrowed_by_the_directory():
    payload = {"sub": "alexey@datatalks.club", "subject": "sub-alex"}
    assert actors.gate(payload) == "operator"
    assert visibility.for_session(payload).is_operator is True


def test_orin_reaches_operator_only_console_routes(monkeypatch):
    from src.dapier.api.admin import routes

    token = _token(monkeypatch, "orin@datatalks.club", "sub-orin")
    event = {"headers": {"host": "dapier.example.test"},
             "cookies": [f"dapier_session={token}"]}
    monkeypatch.setattr(routes, "list_api_tokens",
                        lambda event: {"statusCode": 200, "body": "{}"})
    response = dispatch.route(event, "GET", "/api/admin/tokens")
    assert response["statusCode"] == 200



def test_me_has_no_view_as_directory(monkeypatch):
    for email, sub in (("alexey@datatalks.club", "sub-alex"), ("orin@datatalks.club", "sub-orin")):
        token = _token(monkeypatch, email, sub)
        event = {"headers": {"host": "dapier.example.test"},
                 "cookies": [f"dapier_session={token}"]}
        body = json.loads(dispatch._pre_routes(event, "GET", "/api/admin/me")["body"])
        assert body["role"] == "operator"
        assert "view_as" not in body


def test_no_per_identity_views_remain_in_console_or_cli():
    html = (ROOT / "src/web/index.html").read_text()
    css = (ROOT / "src/web/app.css").read_text()
    assert "view-as" not in html and "view-as" not in css
    assert not (ROOT / "src/web/js/view-as.js").exists()
    runs_cli = (ROOT / "dapier_cli/cli/runs.py").read_text()
    assert "--owner" not in runs_cli and "--assignee" not in runs_cli
