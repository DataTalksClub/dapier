"""Unit tests for dapier slack DM/export helpers and agent routes."""

import json

import pytest

from src.dapier.api.agent import slack as slack_api
from src.dapier.api.agent import slack_ops


class FakeTokens:
    def __init__(self, token="xoxp-" + "a" * 40):
        self.token = token
        self.calls = 0

    def get_access_token(self, connection, **kwargs):
        self.calls += 1
        return self.token, {"expires_at": None}


def _connection(cid="slack-alexey"):
    return {
        "connection_id": cid,
        "provider": "slack",
        "status": "connected",
    }


def test_find_users_filters(monkeypatch):
    monkeypatch.setattr(slack_ops.discovery_api, "_load",
                        lambda cid, table: (_connection(cid), None))
    monkeypatch.setattr(slack_ops.tokens, "get_access_token",
                        FakeTokens().get_access_token)
    monkeypatch.setattr(
        slack_ops.slack_discovery, "_slack_users",
        lambda connection, token, params, limit, **kw: [
            {"id": "U1", "name": "Mikhail Rozhkov", "handle": "mrozhkov",
             "email": "m@example.com"},
            {"id": "U2", "name": "Other", "handle": "other"},
        ],
    )
    payload = slack_ops.find_users("rozhkov", connection_id="slack-alexey")
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == "U1"


def test_open_dm_and_export(monkeypatch):
    monkeypatch.setattr(slack_ops.discovery_api, "_load",
                        lambda cid, table: (_connection(cid), None))
    monkeypatch.setattr(slack_ops.tokens, "get_access_token",
                        FakeTokens().get_access_token)
    monkeypatch.setattr(
        slack_ops.slack_discovery, "_slack_users",
        lambda *a, **k: [{"id": "U1", "name": "Mikhail Rozhkov", "handle": "mrozhkov"}],
    )

    def fake_call(method, token, payload, transport=None):
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D123"}}
        if method == "conversations.history":
            return {
                "ok": True,
                "messages": [
                    {"ts": "1.0", "user": "U1", "text": "hello"},
                    {"ts": "2.0", "user": "Ume", "text": "hi"},
                ],
                "response_metadata": {},
            }
        raise AssertionError(method)

    monkeypatch.setattr(slack_ops.slack_discovery, "_slack_call", fake_call)
    opened = slack_ops.open_dm("rozhkov")
    assert opened["channel"] == "D123"
    exported = slack_ops.dm_export("U1", limit=10)
    assert exported["channel"] == "D123"
    assert exported["count"] == 2
    assert exported["messages"][0]["text"] == "hello"


def test_route_slack_users(monkeypatch):
    calls = {}

    def require_operator(event, action):
        calls["action"] = action
        return "subject-1", None

    monkeypatch.setattr(slack_api, "require_operator", require_operator)
    monkeypatch.setattr(slack_api, "audit", type("A", (), {
        "emit": staticmethod(lambda *a, **k: None)
    })())
    monkeypatch.setattr(
        slack_ops, "find_users",
        lambda query, connection_id="slack-alexey", limit=50: {
            "connection": connection_id, "query": query,
            "items": [{"id": "U1", "name": "A"}], "count": 1,
        },
    )
    # rebind module-level used by find_users_api
    monkeypatch.setattr(slack_api.slack_ops, "find_users",
                        lambda query, connection_id="slack-alexey", limit=50: {
                            "connection": connection_id, "query": query,
                            "items": [{"id": "U1", "name": "A"}], "count": 1,
                        })
    resp = slack_api.route_slack(
        {"queryStringParameters": {"q": "A", "connection_id": "slack-alexey"}},
        "GET", "/api/agent/slack/users",
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["count"] == 1
    assert calls["action"] == "slack.find_users"


def test_route_unmatched():
    assert slack_api.route_slack({}, "GET", "/api/agent/other") is None


def test_cli_help_registers():
    from dapier_cli.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["slack", "find-user", "mikhail", "--connection", "slack-alexey"])
    assert args.group == "slack"
    assert args.command == "find-user"
