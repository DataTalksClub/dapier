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


def _fake_slack(monkeypatch, handler):
    monkeypatch.setattr(slack_ops.discovery_api, "_load",
                        lambda cid, table: (_connection(cid), None))
    monkeypatch.setattr(slack_ops.tokens, "get_access_token",
                        FakeTokens().get_access_token)
    monkeypatch.setattr(slack_ops.slack_discovery, "_slack_call", handler)


def test_history_exposes_reply_count(monkeypatch):
    _fake_slack(monkeypatch, lambda method, token, payload: {
        "ok": True,
        "messages": [{"ts": "1.0", "text": "q", "thread_ts": "1.0",
                      "reply_count": 3, "latest_reply": "9.0"}],
    })
    msg = slack_ops.history("C1")["messages"][0]
    assert msg["reply_count"] == 3 and msg["thread_ts"] == "1.0"


def test_replies_pages_through_cursor(monkeypatch):
    seen = []

    def handler(method, token, payload):
        assert method == "conversations.replies"
        assert payload["ts"] == "1.0"
        seen.append(payload.get("cursor"))
        if not payload.get("cursor"):
            return {"ok": True, "messages": [{"ts": "1.0", "text": "root"}],
                    "response_metadata": {"next_cursor": "c2"}}
        return {"ok": True, "messages": [{"ts": "1.1", "text": "answer"}]}

    _fake_slack(monkeypatch, handler)
    out = slack_ops.replies("C1", "1.0")
    assert [m["text"] for m in out["messages"]] == ["root", "answer"]
    assert seen == [None, "c2"]
    with pytest.raises(slack_ops.SlackOpsError):
        slack_ops.replies("C1", "")


def test_channels_pages_past_one_page(monkeypatch):
    def handler(method, token, payload):
        if not payload.get("cursor"):
            return {"ok": True, "channels": [{"id": "C1", "name": "a"}],
                    "response_metadata": {"next_cursor": "n"}}
        return {"ok": True, "channels": [{"id": "C2", "name": "b", "is_private": True}]}

    _fake_slack(monkeypatch, handler)
    out = slack_ops.channels()
    assert [c["id"] for c in out["items"]] == ["C1", "C2"]
    assert out["items"][1]["type"] == "private"


def test_search_maps_matches(monkeypatch):
    _fake_slack(monkeypatch, lambda method, token, payload: {
        "ok": True,
        "messages": {"matches": [{"ts": "1.0", "text": "t", "user": "U1",
                                   "channel": {"id": "C1", "name": "x"},
                                   "permalink": "p"}],
                     "paging": {"page": 1, "pages": 1, "total": 1}}})
    out = slack_ops.search("in:#x hello")
    assert out["items"][0]["channel_id"] == "C1" and out["total"] == 1


def test_cli_registers_new_slack_commands():
    from dapier_cli.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["slack", "replies", "--channel", "C1", "--thread-ts", "1.0"])
    assert args.command == "replies" and args.thread_ts == "1.0"
    assert parser.parse_args(["slack", "channels"]).command == "channels"
    assert parser.parse_args(["slack", "search", "x"]).command == "search"


@pytest.mark.parametrize("method,form", [
    ("conversations.replies", True), ("search.messages", True),
    ("conversations.history", False)])
def test_slack_call_encoding(method, form):
    from src.dapier.connections import discovery
    seen = {}

    def transport(http_method, url, headers=None, body=None, timeout=None):
        seen["ct"] = headers["content-type"]
        seen["body"] = body
        return 200, b'{"ok": true}'

    discovery._slack_call(method, "t", {"channel": "C1", "limit": 5}, transport=transport)
    if form:
        assert seen["ct"] == "application/x-www-form-urlencoded"
        assert seen["body"] == b"channel=C1&limit=5"
    else:
        assert seen["ct"] == "application/json"
