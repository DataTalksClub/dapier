import json
import os
import stat

import pytest

from dapier_cli import auth, commands, config, main
from dapier_cli.api import ApiError


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


def test_api_url_precedence(isolated_home, monkeypatch):
    assert config.api_url() == "https://dapier.dtcdev.click"
    config.save_api_url("https://custom.example.test/")
    assert config.api_url() == "https://custom.example.test"
    monkeypatch.setenv("DAPIER_API_URL", "https://env.example.test")
    assert config.api_url() == "https://env.example.test"
    assert config.api_url("https://arg.example.test/") == "https://arg.example.test"


def test_session_roundtrip_restricted(isolated_home):
    assert config.load_session() is None
    config.save_session({"id_token": "abc", "subject": "s"})
    assert config.load_session() == {"id_token": "abc", "subject": "s"}
    mode = stat.S_IMODE(os.stat(config.session_file()).st_mode)
    assert mode == 0o600
    assert config.clear_session() is True
    assert config.load_session() is None


def test_pkce_challenge_is_s256():
    import base64
    import hashlib

    verifier, challenge = auth.build_pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected


def test_authorize_url_params():
    url = auth.authorize_url("https://auth.example.test", "cli-id",
                             "http://127.0.0.1:9/callback", "st", "nn", "ch")
    assert "code_challenge=ch" in url
    assert "code_challenge_method=S256" in url
    assert "offline_access" in url
    assert "nonce=nn" in url


VIEW = {
    "connection_id": "youtube-personal",
    "provider": "youtube",
    "expected_account_id": "UC1",
    "verified_account_id": "UC1",
}
TOKEN = {"access_token": "live-token-value", "provider_account_id": "UC1"}


def fake_api(monkeypatch, view=None, token=None):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        if path.startswith("/api/agent/connections/"):
            return dict(view if view is not None else VIEW)
        assert path == "/api/agent/token"
        return dict(token if token is not None else TOKEN)

    monkeypatch.setattr(commands.api, "call", fake_call)
    return calls


def test_token_exec_child_env_only(monkeypatch, capsys):
    captured = {}

    def fake_run(argv, env=None):
        captured["argv"] = argv
        captured["env"] = dict(env)
        assert "live-token-value" not in str(argv)

        class Done:
            returncode = 7

        return Done()

    monkeypatch.setattr(commands.subprocess, "run", fake_run)
    fake_api(monkeypatch)
    code = commands.token_exec("https://api.example.test", "youtube-personal",
                               "buildcamp-uploader", ["printenv"])
    assert code == 7
    assert captured["env"]["YOUTUBE_ACCESS_TOKEN"] == "live-token-value"
    assert captured["env"]["DAPIER_ACCESS_TOKEN"] == "live-token-value"
    assert os.environ.get("YOUTUBE_ACCESS_TOKEN") is None
    assert os.environ.get("DAPIER_ACCESS_TOKEN") is None
    out, _ = capsys.readouterr()
    assert "live-token-value" not in out


def test_token_exec_rejects_account_mismatch(monkeypatch):
    fake_api(monkeypatch, token={"access_token": "t", "provider_account_id": "UC-EVIL"})
    with pytest.raises(ApiError):
        commands.token_exec("https://api.example.test", "youtube-personal",
                            "buildcamp-uploader", ["cmd"])


def test_token_exec_rejects_unverified_connection(monkeypatch):
    view = dict(VIEW, expected_account_id=None, verified_account_id=None)
    fake_api(monkeypatch, view=view)
    with pytest.raises(ApiError):
        commands.token_exec("https://api.example.test", "youtube-personal",
                            "buildcamp-uploader", ["cmd"])


def test_token_write_private_file(monkeypatch, tmp_path, capsys):
    fake_api(monkeypatch)
    output = str(tmp_path / "token.txt")
    assert commands.token_write("https://api.example.test", "youtube-personal",
                                "buildcamp-uploader", output) == 0
    assert open(output).read() == "live-token-value\n"
    assert stat.S_IMODE(os.stat(output).st_mode) == 0o600
    out, _ = capsys.readouterr()
    assert out.strip() == output
    assert "live-token-value" not in out
    # Refuses to overwrite by default.
    assert commands.token_write("https://api.example.test", "youtube-personal",
                                "buildcamp-uploader", output) == 2
    assert commands.token_write("https://api.example.test", "youtube-personal",
                                "buildcamp-uploader", output, force=True) == 0


def test_main_auth_status_exit_codes(isolated_home, capsys):
    assert main.main(["auth", "status"]) == 3
    config.save_session({"id_token": "x", "subject": "s", "email": "e",
                         "expires_at": 9_999_999_999})
    assert main.main(["auth", "status"]) == 0
    out, _ = capsys.readouterr()
    assert "id_token" not in out and "\"x\"" not in out


def test_main_token_exec_parsing(monkeypatch):
    seen = {}

    def fake_exec(api_url, connection_id, agent, argv, debug=False):
        seen.update(connection_id=connection_id, agent=agent, argv=argv)
        return 0

    monkeypatch.setattr(commands, "token_exec", fake_exec)
    assert main.main(["token", "exec", "youtube-personal", "--agent", "buildcamp-uploader",
                      "--", "echo", "hi"]) == 0
    assert seen == {"connection_id": "youtube-personal", "agent": "buildcamp-uploader",
                    "argv": ["echo", "hi"]}


def test_import_rejects_missing_files(isolated_home):
    assert commands.connections_import("https://api.example.test", "c", "youtube",
                                       "cid", str(isolated_home / "nope"),
                                       str(isolated_home / "nope2")) == 2


def test_import_posts_files_without_logging(monkeypatch, tmp_path, capsys):
    secret_file = tmp_path / "secret"
    secret_file.write_text("client-secret-value\n")
    user_file = tmp_path / "user.json"
    user_file.write_text(json.dumps({"refresh_token": "refresh-value"}))
    posted = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        posted.update(body=body, path=path)
        return {"connection_id": "youtube-datatalksclub", "verified_account_id": "UC1"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    code = commands.connections_import(
        "https://api.example.test", "youtube-datatalksclub", "youtube",
        "client-id", str(secret_file), str(user_file),
        expected_account_id="UC1", scopes=["s1"],
    )
    assert code == 0
    assert posted["path"] == "/api/admin/connections/import"
    assert posted["body"]["authorized_user"] == {"refresh_token": "refresh-value"}
    assert posted["body"]["client_secret"] == "client-secret-value"
    out, _ = capsys.readouterr()
    assert "refresh-value" not in out
    assert "client-secret-value" not in out


TRIGGER = {
    "name": "consulting",
    "address": "consulting@dtcdev.click",
    "description": "consulting invoices",
    "enabled": True,
    "actions": [{"type": "dropbox_upload", "connection_id": "dropbox", "folder": "/x"}],
}


def test_triggers_list_and_show(monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/email-triggers")
        return {"domain": "dtcdev.click", "triggers": [TRIGGER], "yaml_routes": ["todo"]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.triggers_list("https://api.example.test") == 0
    out, _ = capsys.readouterr()
    assert "consulting@dtcdev.click" in out
    assert "dropbox_upload" in out
    assert "todo" in out
    assert commands.triggers_show("https://api.example.test", "consulting") == 0
    out, _ = capsys.readouterr()
    assert '"type": "dropbox_upload"' in out
    assert commands.triggers_show("https://api.example.test", "missing") == 4


def test_triggers_save_and_delete(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"created": method == "PUT", "name": "consulting",
                "address": "consulting@dtcdev.click"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    trigger_file = tmp_path / "trigger.json"
    trigger_file.write_text(json.dumps({"name": "consulting", "actions": TRIGGER["actions"]}))
    assert commands.triggers_save("https://api.example.test", str(trigger_file)) == 0
    assert (seen["method"], seen["path"]) == ("PUT", "/api/agent/email-triggers")
    assert seen["body"] == {"name": "consulting", "actions": TRIGGER["actions"]}
    out, _ = capsys.readouterr()
    assert "Created consulting@dtcdev.click" in out
    assert commands.triggers_save("https://api.example.test", str(tmp_path / "nope")) == 2
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not json")
    assert commands.triggers_save("https://api.example.test", str(bad_file)) == 2
    assert commands.triggers_delete("https://api.example.test", "consulting") == 0
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/api/agent/email-triggers?name=consulting"
    out, _ = capsys.readouterr()
    assert "Deleted consulting@dtcdev.click" in out


def test_main_triggers_parsing(monkeypatch):
    seen = {}

    def fake_save(api_url, path, debug=False):
        seen["file"] = path
        return 0

    monkeypatch.setattr(commands, "triggers_save", fake_save)
    assert main.main(["triggers", "save", "/tmp/trigger.json"]) == 0
    assert seen["file"] == "/tmp/trigger.json"
