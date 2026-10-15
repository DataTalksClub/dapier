import io
import sys
import pytest
from plugins.typefully import plugin
from src.dapier.connections import credentials
from dapier_cli.commands.credentials import credentials_set
from dapier_cli import api


def test_save_is_write_only_and_rejects_bad_keys(monkeypatch):
    saved = []
    monkeypatch.setattr(credentials, "put_credential", lambda *args, **kwargs: saved.append((args, kwargs)))
    key = "test-key-" + "x" * 40
    status, body = credentials.api_save_credential("typefully", {"api_key": key})
    assert status == 200 and body == {"provider": "typefully", "configured": True}
    assert saved[0][0] == ("typefully", {"api_key": key})
    for invalid in [None, {}, "short", "x" * 25 + "\nheader: injected", "x" * 2049]:
        assert credentials.api_save_credential("typefully", {"api_key": invalid})[0] == 400
    assert len(saved) == 1


def test_cli_uses_shared_api_without_printing_secret(monkeypatch, capsys):
    key = "test-key-" + "z" * 40
    monkeypatch.setattr(sys, "stdin", io.StringIO(key))
    calls = []
    monkeypatch.setattr(api, "call", lambda *args, **kwargs: calls.append(args) or {"provider": "typefully"})
    assert credentials_set("https://example.test", "typefully", "-") == 0
    assert calls[0][1:] == ("PUT", "/api/agent/credentials/typefully", {"api_key": key})
    assert key not in capsys.readouterr().out


def test_request_scopes_bearer_to_fixed_provider(monkeypatch):
    monkeypatch.setattr(credentials, "get_credential", lambda _: {"api_key": "secret"})
    calls = []
    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return 200, b'{"id": 7}'
    assert plugin.request("/me", transport=transport) == {"id": 7}
    assert calls[0][0:2] == ("GET", "https://api.typefully.com/v2/me")
    assert calls[0][2]["headers"]["authorization"] == "Bearer secret"


def test_discovery_paginates_without_trusting_next_url(monkeypatch):
    seen = []
    def request(path, connection):
        seen.append(path)
        return {"results": [{"id": len(seen), "name": "Brand"}], "next": "https://evil.test" if len(seen) == 1 else None}
    monkeypatch.setattr(plugin, "request", request)
    assert [i["id"] for i in plugin.social_sets({}, {})] == ["1", "2"]
    assert seen == ["/social-sets?limit=50&offset=0", "/social-sets?limit=50&offset=50"]


def test_health_never_echoes_transport_secrets(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("credential-value")
    monkeypatch.setattr(plugin, "request", fail)
    assert plugin.health({})["ok"] is False
    assert "credential-value" not in str(plugin.health({}))


def test_discovery_and_console_share_registered_provider():
    from src.dapier.api.discovery import _pseudo_connection
    assert _pseudo_connection("typefully")["provider"] == "typefully"
