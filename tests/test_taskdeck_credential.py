"""Taskdeck's secret follows the same stored-credential API as other providers."""
import json

from dapier_cli.commands import credentials as cli
from src.dapier.connections import credentials
from src.dapier.triggers import taskdeck_status


def test_taskdeck_credential_is_validated_and_write_only(monkeypatch):
    writes = []
    monkeypatch.setattr(credentials, "put_credential", lambda *args, **kwargs: writes.append((args, kwargs)))
    assert credentials.api_save_credential("taskdeck", {"token": "short"})[0] == 400
    secret = "test-" + "a" * 40
    status, body = credentials.api_save_credential("taskdeck", {"token": secret})
    assert status == 200
    assert secret not in json.dumps(body)
    assert writes == [(("taskdeck", {"token": secret}), {"provider": "taskdeck"})]


def test_cli_taskdeck_uses_shared_credential_endpoint(monkeypatch, tmp_path, capsys):
    secret = "test-" + "a" * 40
    path = tmp_path / "secret"
    path.write_text(secret)
    calls = []
    monkeypatch.setattr(cli.api, "call", lambda *args, **kwargs: calls.append(args) or {"provider": "taskdeck"})
    assert cli.credentials_set("https://api.test", "taskdeck", str(path)) == 0
    assert calls == [("https://api.test", "PUT", "/api/agent/credentials/taskdeck", {"token": secret})]
    assert secret not in capsys.readouterr().out


def test_taskdeck_environment_token_cannot_bypass_unconfigured_store(monkeypatch):
    monkeypatch.setenv("TASKDECK_STATUS_TOKEN", "legacy-env-secret")
    monkeypatch.setattr(credentials, "get_credential", lambda key: {})
    assert taskdeck_status._authorised({"headers": {"Authorization": "Bearer legacy-env-secret"}}) is False


def test_taskdeck_storage_failure_fails_closed(monkeypatch):
    def unavailable(key):
        raise OSError("credentials unavailable")
    monkeypatch.setattr(credentials, "get_credential", unavailable)
    assert taskdeck_status._authorised({"headers": {"Authorization": "Bearer arbitrary"}}) is False


def test_taskdeck_credential_api_paths_share_validation_and_storage(monkeypatch):
    from src.dapier.api import admin
    from src.dapier.api import agent as agent_api
    from src.dapier.auth import session
    from tests.test_agent_api import configure, event

    configure(monkeypatch, claims={"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setattr(session, "_session_subject", lambda event: "op-1")
    writes = []
    monkeypatch.setattr(credentials, "put_credential", lambda *args, **kwargs: writes.append((args, kwargs)))
    secret = "test-" + "a" * 40
    agent_result = agent_api.route(event({"token": secret}), "PUT", "/api/agent/credentials/taskdeck")
    admin_result = admin.save_credential("taskdeck", {"body": json.dumps({"token": secret})})
    assert agent_result["statusCode"] == admin_result["statusCode"] == 200
    assert secret not in agent_result["body"] and secret not in admin_result["body"]
    assert writes[0] == writes[1]
