"""CLI storage commands: thin clients over /api/agent/storage/{workflow}.

Parity with the console and the engine (AGENTS.md): the CLI never touches
DynamoDB — these tests pin the endpoints it calls and the rendering.
"""
import pytest

from dapier_cli import commands, main


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def test_storage_set_get_find_delete_hit_the_agent_endpoints(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "POST":
            return {"workflow": "wf-1", "key": "cursor", "stored": True,
                    "updated_at": "2026-09-28T00:00:00+00:00"}
        if method == "GET" and "key=" in path:
            return {"workflow": "wf-1", "key": "cursor", "value": "inbox/42",
                    "updated_at": "2026-09-28T00:00:00+00:00"}
        if method == "GET":
            return {"workflow": "wf-1", "prefix": "seen/",
                    "items": [{"key": "seen/a", "value": "1"},
                              {"key": "seen/b", "value": "2"}], "count": 2}
        return {"workflow": "wf-1", "key": "cursor", "deleted": True}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["storage", "set", "wf-1", "cursor", "inbox/42",
                      "--ttl-seconds", "3600"]) == 0
    assert calls[-1] == ("POST", "/api/agent/storage/wf-1",
                         {"key": "cursor", "value": "inbox/42", "ttl_seconds": 3600})

    assert main.main(["storage", "get", "wf-1", "cursor"]) == 0
    assert calls[-1][:2] == ("GET", "/api/agent/storage/wf-1?key=cursor")
    out = capsys.readouterr().out
    assert "cursor = inbox/42" in out

    assert main.main(["storage", "find", "wf-1", "seen/", "--limit", "5"]) == 0
    assert calls[-1][:2] == ("GET", "/api/agent/storage/wf-1?prefix=seen%2F&limit=5")
    out = capsys.readouterr().out
    assert "seen/a" in out and "seen/b" in out

    assert main.main(["storage", "delete", "wf-1", "cursor"]) == 0
    assert calls[-1][:2] == ("DELETE", "/api/agent/storage/wf-1?key=cursor")
    out = capsys.readouterr().out
    assert "Deleted cursor" in out


def test_storage_delete_of_a_missing_key_says_so(isolated_home, monkeypatch, capsys):
    monkeypatch.setattr(
        commands.api, "call",
        lambda api_url, method, path, body=None, **kwargs:
        {"workflow": "wf-1", "key": "gone", "deleted": False})
    assert main.main(["storage", "delete", "wf-1", "gone"]) == 0
    assert "nothing to delete" in capsys.readouterr().out


def test_storage_get_of_a_missing_key_reports_the_404(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        raise commands.api.ApiError("No stored value for key 'nope'", status=404)

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert main.main(["storage", "get", "wf-1", "nope"]) == 4
    assert "No stored value" in capsys.readouterr().out
