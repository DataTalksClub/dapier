"""`dapier triggers sample`: the CLI's trigger sample pull (thin client).

The command posts to /api/agent/discover — the same dispatch the console's
designer test panel uses — and renders the pulled sample; --resource lists
a field's options instead. API errors surface through the shared error
path (exit 4), like every other CLI command.
"""
import json

import pytest

from dapier_cli import commands, main
from dapier_cli.api import ApiError


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def test_triggers_sample_pulls_and_prints(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"connector": "schedule", "source": "synthetic",
                "connection_id": None,
                "sample": {"connector": "schedule", "event": "schedule.triggered",
                           "occurred_at": "2026-09-27T03:00:00Z",
                           "data": {"schedule": "weekly",
                                    "utc_time": "2026-09-27T03:00:00Z"}}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["triggers", "sample", "schedule"]) == 0

    assert calls == [("POST", "/api/agent/discover",
                      {"connector": "schedule", "kind": "sample"})]
    out = capsys.readouterr().out
    assert "schedule.triggered" in out
    assert "synthetic" in out
    assert '"schedule": "weekly"' in out


def test_triggers_sample_forwards_event_and_connection(isolated_home, monkeypatch, capsys):
    captured = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        captured["body"] = body
        return {"connector": "poll", "source": "live", "connection_id": "conn-1",
                "sample": {"connector": "poll", "event": "item.new",
                           "occurred_at": "2026-09-27T03:00:00Z",
                           "data": {"title": "New post", "poll": "blog"}}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["triggers", "sample", "poll", "--event", "blog",
                      "--connection-id", "conn-1"]) == 0

    assert captured["body"] == {"connector": "poll", "kind": "sample",
                                "event": "blog", "connection_id": "conn-1"}
    assert "item.new" in capsys.readouterr().out


def test_triggers_sample_resource_lists_options(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert body == {"connector": "slack", "kind": "options",
                        "resource": "slack.channels", "limit": 3}
        return {"connector": "slack", "resource": "slack.channels",
                "connection_id": "slack-main",
                "options": [{"value": "C123", "label": "general"},
                            {"value": "C456", "label": "random"}]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["triggers", "sample", "slack",
                      "--resource", "slack.channels", "--limit", "3"]) == 0

    out = capsys.readouterr().out
    assert "slack.channels" in out and "slack-main" in out
    assert "C123" in out and "general" in out and "random" in out


def test_triggers_sample_reports_unknown_connector(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        raise ApiError("unknown discovery connector 'bogus'; discoverable: email, ...",
                       status=404)

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["triggers", "sample", "bogus"]) == 4
    assert "unknown discovery connector" in capsys.readouterr().out
