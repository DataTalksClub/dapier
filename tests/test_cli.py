import base64
import subprocess
import sys
import time
import webbrowser
import io
import json
import os
import stat
import zipfile

import pytest

from dapier_cli import auth, commands, config, main
from dapier_cli.cli import auth as cli_auth
from dapier_cli import commands as cli_commands
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
    if os.name == "nt":
        from src.dapier.private_files import require_private
        require_private(config.session_file())
    else:
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
    import urllib.parse

    url = auth.authorize_url("https://auth.example.test", "cli-id",
                             auth.REDIRECT_URI, "st", "nn", "ch")
    assert "code_challenge=ch" in url
    assert "code_challenge_method=S256" in url
    assert f"redirect_uri={urllib.parse.quote(auth.REDIRECT_URI, safe='')}" in url
    assert "nonce=nn" in url


def test_redirect_uri_satisfies_cognito_loopback_rules():
    # Cognito whitelists cleartext loopback redirects only as http://localhost
    # on a fixed port; the shared-auth stack registers exactly this URL.
    import urllib.parse

    parts = urllib.parse.urlparse(auth.REDIRECT_URI)
    assert parts.scheme == "http"
    assert parts.hostname == "localhost"
    assert parts.port == auth.LOOPBACK_PORT and parts.path == "/callback"


def device_poster(script):
    """Scripted _post_json stand-in keyed by endpoint suffix."""
    calls = []

    def poster(url, body, bearer=None, timeout=20):
        calls.append({"url": url, "body": body, "bearer": bearer})
        for suffix, response in script.items():
            if url.endswith(suffix):
                return response() if callable(response) else response
        raise AssertionError(f"unexpected POST {url}")

    return poster, calls


def test_login_device_happy_path(isolated_home, monkeypatch, capsys):
    poster, calls = device_poster({
        "/device/start": (200, {"device_code": "dapd_secret", "user_code": "ABCD-EFGH",
                                "expires_in": 900, "interval": 2}),
        "/device/token": (200, {"status": "approved", "token": "dapd_tok",
                                "subject": "Google_1", "email": "op@datatalks.club",
                                "expires_at": 4102444800}),
    })
    monkeypatch.setattr(auth, "_post_json", poster)

    session = auth.login_device("https://api.example.test/", sleeper=lambda seconds: None)

    assert session == {"token": "dapd_tok", "kind": "device", "subject": "Google_1",
                       "email": "op@datatalks.club", "expires_at": 4102444800,
                       "obtained_at": session["obtained_at"]}
    assert config.load_session() == session
    assert calls[0]["url"] == "https://api.example.test/api/agent/device/start"
    assert calls[1]["body"] == {"device_code": "dapd_secret"}
    output = capsys.readouterr().out
    assert "https://api.example.test/device" in output
    assert "valid for 15 minutes" in output


def test_login_device_waits_out_the_code_life():
    # The CLI must never give up before the code does: a 15-minute pairing is
    # waited out in full even if --timeout were still at the old 10-minute
    # default; without a server-reported expiry, --timeout rules as before.
    assert auth._wait_seconds(600, 900) == 915
    assert auth._wait_seconds(900, 900) == 915
    assert auth._wait_seconds(30, None) == 30
    assert auth._wait_seconds(0, 0) == 0


def test_login_timeout_default_matches_the_code_life():
    args = main.build_parser().parse_args(["auth", "login"])
    assert args.timeout == 900


def test_login_device_polls_until_approved(isolated_home, monkeypatch):
    polls = []

    def poster(url, body, bearer=None, timeout=20):
        if url.endswith("/device/start"):
            return 200, {"device_code": "dapd_secret", "user_code": "ABCD-EFGH"}
        if url.endswith("/device/token"):
            polls.append(body)
            if len(polls) >= 3:
                return (200, {"status": "approved", "token": "dapd_tok",
                              "subject": "Google_1", "email": "op@x", "expires_at": 4102444800})
            return (200, {"status": "pending"})
        raise AssertionError(url)

    monkeypatch.setattr(auth, "_post_json", poster)
    sleeps = []
    session = auth.login_device("https://api.example.test", sleeper=sleeps.append, timeout=30)
    assert session["token"] == "dapd_tok"
    assert len(polls) == 3 and len(sleeps) == 2


def test_login_device_expired_and_timeout(isolated_home, monkeypatch):
    poster, _ = device_poster({
        "/device/start": (200, {"device_code": "dapd_secret", "user_code": "ABCD-EFGH"}),
        "/device/token": (200, {"status": "expired"}),
    })
    monkeypatch.setattr(auth, "_post_json", poster)
    with pytest.raises(auth.LoginError, match="expired"):
        auth.login_device("https://api.example.test", sleeper=lambda s: None)

    def pending_forever(url, body, bearer=None, timeout=20):
        if url.endswith("/device/start"):
            return 200, {"device_code": "dapd_secret", "user_code": "ABCD-EFGH"}
        return (200, {"status": "pending"})

    monkeypatch.setattr(auth, "_post_json", pending_forever)
    with pytest.raises(auth.LoginError, match="Timed out"):
        auth.login_device("https://api.example.test", sleeper=lambda s: None, timeout=0)


def test_login_device_honors_slow_down(isolated_home, monkeypatch):
    poster, _ = device_poster({
        "/device/start": (200, {"device_code": "dapd_secret", "user_code": "ABCD-EFGH"}),
        "/device/token": (200, {"status": "pending", "slow_down": True}),
    })
    monkeypatch.setattr(auth, "_post_json", poster)
    sleeps = []

    def sleeper(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise KeyboardInterrupt  # stop the wait loop after two intervals

    with pytest.raises(KeyboardInterrupt):
        auth.login_device("https://api.example.test", sleeper=sleeper, timeout=60)
    assert sleeps == [4, 6]


def test_refresh_device_session_rotates(isolated_home, monkeypatch):
    config.save_session({"token": "dapd_old", "kind": "device", "subject": "s",
                         "email": "e", "expires_at": 1, "obtained_at": 1})
    poster, calls = device_poster({
        "/device/refresh": (200, {"token": "dapd_new", "expires_at": 4102444800}),
    })
    monkeypatch.setattr(auth, "_post_json", poster)

    refreshed = auth.refresh_session("https://api.example.test", config.load_session())

    assert refreshed["token"] == "dapd_new"
    assert calls[0]["bearer"] == "dapd_old"
    assert config.load_session()["token"] == "dapd_new"


def test_refresh_dt_session_still_uses_cognito(isolated_home, monkeypatch):
    config.save_session({"id_token": "idt", "refresh_token": "rt", "kind": "dtc",
                         "subject": "s", "expires_at": 1, "obtained_at": 1})
    monkeypatch.setattr(auth, "server_config", lambda api_url: {
        "auth_base_url": "https://auth.example.test", "cli_client_id": "cid",
        "issuer": "iss", "jwks_url": "jwks"})
    monkeypatch.setattr(auth, "_post_form", lambda url, fields, timeout=20: (
        200, b'{"id_token": "idt2"}'))
    monkeypatch.setattr(auth, "verify_session_token", lambda *a, **k: {"sub": "s"})

    refreshed = auth.refresh_session("https://api.example.test", config.load_session())

    assert refreshed["id_token"] == "idt2"
    assert config.load_session()["id_token"] == "idt2"


def test_api_call_sends_device_bearer(isolated_home, monkeypatch):
    config.save_session({"token": "dapd_b", "kind": "device", "subject": "s", "expires_at": 9})
    seen = {}

    def fake_request(api_url, method, path, session, body=None, timeout=20, debug=False):
        from dapier_cli import api
        seen["bearer"] = api.session_bearer(session)
        return 200, {}

    monkeypatch.setattr(commands.api, "_request", fake_request)
    commands.api.call("https://api.example.test", "GET", "/api/agent/connections")
    assert seen["bearer"] == "dapd_b"

    config.save_session({"id_token": "idt"})  # legacy loopback session
    monkeypatch.setattr(commands.api, "_request", fake_request)
    commands.api.call("https://api.example.test", "GET", "/api/agent/connections")
    assert seen["bearer"] == "idt"


def test_login_falls_back_when_device_flow_unavailable(isolated_home, monkeypatch, capsys):
    def unavailable(url, body, bearer=None, timeout=20):
        raise auth.DeviceFlowUnavailable

    monkeypatch.setattr(auth, "_post_json", unavailable)
    monkeypatch.setattr(auth, "login", lambda api_url, timeout=180: {"email": "op@x"})

    import argparse
    code = cli_auth.run(argparse.Namespace(command="login", timeout=10, browser=False),
                         "https://api.example.test")

    assert code == 0
    assert "falling back" in capsys.readouterr().out


def test_logout_revokes_device_session(isolated_home, monkeypatch):
    config.save_session({"token": "dapd_bye", "kind": "device", "subject": "s",
                         "email": "e", "expires_at": 9, "obtained_at": 1})
    revoked = []
    monkeypatch.setattr(auth, "revoke_session",
                        lambda api_url, session: revoked.append(session["token"]))

    import argparse
    code = cli_auth.run(argparse.Namespace(command="logout"), "https://api.example.test")

    assert code == 0
    assert revoked == ["dapd_bye"]
    assert config.load_session() is None


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

    monkeypatch.setattr(subprocess, "run", fake_run)
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
    if os.name == "nt":
        from pathlib import Path
        from src.dapier.private_files import require_private
        require_private(Path(output))
    else:
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
    monkeypatch.setattr(commands, "resolve_connection_id",
                        lambda api_url, words, agent=None, debug=False: " ".join(words))

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
    assert posted["path"] == "/api/agent/connections/import"
    assert posted["body"]["authorized_user"] == {"refresh_token": "refresh-value"}
    assert posted["body"]["client_secret"] == "client-secret-value"
    out, _ = capsys.readouterr()
    assert "refresh-value" not in out
    assert "client-secret-value" not in out


def test_import_prints_youtube_hub_setup(monkeypatch, tmp_path, capsys):
    user_file = tmp_path / "user.json"
    user_file.write_text(json.dumps({"refresh_token": "r"}))

    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"connection_id": "youtube", "account_title": "My channel"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_import(
        "https://api.example.test", "youtube", "youtube", None, None,
        str(user_file)) == 0
    out, _ = capsys.readouterr()
    assert "pubsubhubbub.appspot.com/subscribe" in out
    assert "https://api.example.test/hooks/youtube" in out
    assert "videos.xml?channel_id=" in out


def test_import_prints_dropbox_webhook_setup(monkeypatch, tmp_path, capsys):
    user_file = tmp_path / "user.json"
    user_file.write_text(json.dumps({"refresh_token": "r"}))

    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"connection_id": "dropbox", "account_title": "Team Dropbox"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_import(
        "https://api.example.test", "dropbox", "dropbox", None, None,
        str(user_file)) == 0
    out, _ = capsys.readouterr()
    assert "https://api.example.test/hooks/dropbox" in out
    assert "Webhook URI" in out


def test_connect_prints_provider_webhook_setup(monkeypatch, capsys):
    responses = [
        {"authorize_url": "https://oauth.example.test/authorize"},
        {"connection_id": "yt", "status": "connected", "provider": "youtube",
         "account_title": "My channel"},
    ]
    monkeypatch.setattr(commands.api, "call", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(webbrowser, "open", lambda url: True)
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    assert commands.connections_connect("https://api.example.test", "yt", "agent-1") == 0
    out, _ = capsys.readouterr()
    assert "/hooks/youtube" in out
    assert "pubsubhubbub.appspot.com/subscribe" in out


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
        return {"addresses": [
            {"name": "consulting", "address": "consulting@dtcdev.click", "handlers": [{"workflow": "consulting-flow", "status": "enabled", "action_types": ["dropbox_upload"]}]},
            {"name": "todo", "address": "todo@dtcdev.click", "handlers": [{"workflow": "todo-email-sheet", "status": "enabled", "action_types": []}]}]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.triggers_list("https://api.example.test") == 0
    out, _ = capsys.readouterr()
    assert "consulting@dtcdev.click" in out
    assert "dropbox_upload" in out
    assert "todo@dtcdev.click" in out
    assert "workflow=todo-email-sheet" in out
    assert commands.triggers_show("https://api.example.test", "consulting") == 0
    out, _ = capsys.readouterr()
    assert "dropbox_upload" in out
    assert commands.triggers_show("https://api.example.test", "missing") == 4


def test_credentials_set_posts_value_without_echoing(monkeypatch, tmp_path, capsys):
    posted = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        posted.update(method=method, path=path, body=body)
        return {"provider": "slack", "configured": True}

    monkeypatch.setattr(commands.api, "call", fake_call)
    secret = tmp_path / "slack-token"
    secret.write_text("xoxb-123456789012345678901234\n")
    assert commands.credentials_set("https://api.example.test", "slack", str(secret)) == 0
    assert posted == {"method": "PUT", "path": "/api/agent/credentials/slack",
                      "body": {"token": "xoxb-123456789012345678901234"}}
    out, _ = capsys.readouterr()
    assert "xoxb-" not in out


def test_credentials_set_reads_stdin(monkeypatch, capsys):
    posted = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        posted.update(path=path, body=body)
        return {"provider": "mailchimp", "configured": True}

    monkeypatch.setattr(commands.api, "call", fake_call)
    monkeypatch.setattr(sys, "stdin", io.StringIO("a" * 20 + "-us1\n"))
    assert commands.credentials_set("https://api.example.test", "mailchimp", "-") == 0
    assert posted == {"path": "/api/agent/credentials/mailchimp",
                      "body": {"api_key": "a" * 20 + "-us1"}}


def test_credentials_set_rejects_unknown_provider_and_empty_value(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(commands.api, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert commands.credentials_set("https://api.example.test", "youtube", "-") == 2
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    assert commands.credentials_set("https://api.example.test", "slack", str(empty)) == 2
    assert commands.credentials_set("https://api.example.test", "slack",
                                    str(tmp_path / "missing")) == 2


def test_credentials_set_aws_posts_the_key_pair_json(monkeypatch, tmp_path, capsys):
    posted = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        posted.update(method=method, path=path, body=body)
        return {"provider": "aws", "configured": True}

    monkeypatch.setattr(commands.api, "call", fake_call)
    keys = tmp_path / "aws.json"
    keys.write_text(json.dumps({"access_key_id": "AKIAIOSFODNN7EXAMPLE",
                                "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}))
    assert commands.credentials_set("https://api.example.test", "aws", str(keys)) == 0
    assert posted == {"method": "PUT", "path": "/api/agent/credentials/aws",
                      "body": {"access_key_id": "AKIAIOSFODNN7EXAMPLE",
                               "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}}
    out, _ = capsys.readouterr()
    assert "AKIA" not in out


def test_credentials_set_aws_rejects_non_json_and_wrong_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(commands.api, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    raw = tmp_path / "raw.txt"
    raw.write_text("AKIAIOSFODNN7EXAMPLE")
    assert commands.credentials_set("https://api.example.test", "aws", str(raw)) == 2
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"access_key_id": "AKIAIOSFODNN7EXAMPLE"}))
    assert commands.credentials_set("https://api.example.test", "aws", str(partial)) == 2


GRANT_BODY = {
    "connection_id": "youtube-personal",
    "subject": "subject-9",
    "agent": "buildcamp-uploader",
    "operations": ["use"],
}


def test_grants_commands_hit_agent_endpoints(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "GET":
            return {"grants": [dict(GRANT_BODY, grantee="subject-9#buildcamp-uploader")]}
        if method == "PUT":
            return dict(GRANT_BODY, grantee="subject-9#buildcamp-uploader", granted_by="op-1")
        return {"ok": True}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.grants_list("https://api.example.test") == 0
    assert commands.grants_list("https://api.example.test", connection_id="youtube-personal") == 0
    grant_file = tmp_path / "grant.json"
    grant_file.write_text(json.dumps(GRANT_BODY))
    assert commands.grants_save("https://api.example.test", str(grant_file)) == 0
    assert commands.grants_delete("https://api.example.test", "youtube-personal",
                                  "subject-9#buildcamp-uploader") == 0
    assert calls == [
        ("GET", "/api/agent/grants", None),
        ("GET", "/api/agent/grants?connection_id=youtube-personal", None),
        ("PUT", "/api/agent/grants", GRANT_BODY),
        ("DELETE", "/api/agent/grants?connection_id=youtube-personal"
                   "&grantee=subject-9%23buildcamp-uploader", None),
    ]


def test_connections_revoke(monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("DELETE", "/api/agent/connections/youtube-personal/tokens")
        return {"connection_id": "youtube-personal", "status": "revoked"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_revoke("https://api.example.test", "youtube-personal") == 0
    out, _ = capsys.readouterr()
    assert "revoked" in out


def test_overview_prints_operator_summary(monkeypatch, capsys):
    payload = {
        "service": "dapier",
        "region": "eu-west-1",
        "workflows": [{"id": "demo", "enabled": True}, {"id": "retired", "enabled": False}],
        "connections": [{"connection_id": "youtube-personal", "provider": "youtube",
                         "status": "connected"}],
        "credentials": [{"provider": "slack", "configured": True,
                         "updated_at": "2026-09-24T00:00:00+00:00"},
                        {"provider": "mailchimp", "configured": False, "updated_at": None}],
        "executions": [{"workflow_id": "demo", "action_id": "upload", "status": "completed",
                        "started_at": "2026-09-24T01:00:00+00:00"}],
    }
    monkeypatch.setattr(commands.api, "call",
                        lambda api_url, method, path, body=None, **kwargs: (
                            pytest.fail("wrong path") if path != "/api/agent/overview" else payload))
    assert commands.overview("https://api.example.test") == 0
    out, _ = capsys.readouterr()
    assert "dapier in eu-west-1" in out
    assert "demo" in out and "retired" in out
    assert "youtube-personal" in out
    assert "configured" in out and "not set" in out
    assert "completed" in out


def test_main_operator_command_parsing(monkeypatch):
    seen = {}
    monkeypatch.setattr(commands, "resolve_connection_id",
                        lambda api_url, words, agent=None, debug=False: " ".join(words))

    monkeypatch.setattr(commands, "grants_delete",
                        lambda api_url, connection_id, grantee, debug=False:
                        seen.update(connection_id=connection_id, grantee=grantee) or 0)
    assert main.main(["grants", "delete", "youtube-personal", "subject-9#uploader"]) == 0
    assert seen == {"connection_id": "youtube-personal", "grantee": "subject-9#uploader"}

    monkeypatch.setattr(commands, "credentials_set",
                        lambda api_url, provider, path, debug=False:
                        seen.update(provider=provider, path=path) or 0)
    assert main.main(["credentials", "set", "mailchimp", "--file", "-"]) == 0
    assert seen["provider"] == "mailchimp" and seen["path"] == "-"

    monkeypatch.setattr(commands, "overview", lambda api_url, debug=False, section=None: 0)
    assert main.main(["overview"]) == 0

    monkeypatch.setattr(commands, "connections_revoke",
                        lambda api_url, connection_id, debug=False:
                        seen.update(revoked=connection_id) or 0)
    assert main.main(["connections", "revoke", "youtube-personal"]) == 0
    assert seen["revoked"] == "youtube-personal"

    monkeypatch.setattr(commands, "connections_create",
                        lambda api_url, connection_id, provider, scopes, **kwargs:
                        seen.update(created=connection_id, provider=provider, create_scopes=scopes) or 0)
    assert main.main(["connections", "create", "youtube-team", "--provider", "youtube",
                      "--scopes", "https://www.googleapis.com/auth/youtube.readonly"]) == 0
    assert seen["created"] == "youtube-team" and seen["provider"] == "youtube"

    monkeypatch.setattr(commands, "connections_edit",
                        lambda api_url, connection_id, **kwargs:
                        seen.update(edited=connection_id, edit_kwargs=kwargs) or 0)
    assert main.main(["connections", "edit", "dropbox", "--clear-root-path"]) == 0
    assert seen["edited"] == "dropbox" and seen["edit_kwargs"]["root_path"] == ""


HOOK = {
    "hook_id": "orders",
    "kind": "webhook",
    "url": "https://dapier.example.test/hooks/webhook/orders",
    "token": "hook-token-value",
    "header": "authorization",
    "enabled": True,
    "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
}


def test_hooks_list_and_show(monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/hook-triggers")
        return {"base_url": "https://dapier.example.test", "hooks": [HOOK]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.hooks_list("https://api.example.test") == 0
    out, _ = capsys.readouterr()
    assert "/hooks/webhook/orders" in out
    assert commands.hooks_show("https://api.example.test", "orders") == 0
    out, _ = capsys.readouterr()
    assert "Bearer hook-token-value" in out
    assert commands.hooks_show("https://api.example.test", "missing") == 4


def test_hooks_save_prints_caller_instructions(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"created": True, "hook_id": "orders", "kind": "webhook",
                "url": HOOK["url"], "token": "tok", "header": "authorization"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    hook_file = tmp_path / "hook.json"
    hook_file.write_text(json.dumps({"kind": "webhook", "name": "orders", "actions": HOOK["actions"]}))
    assert commands.hooks_save("https://api.example.test", str(hook_file)) == 0
    assert (seen["method"], seen["path"]) == ("PUT", "/api/agent/hook-triggers")
    out, _ = capsys.readouterr()
    assert "Created webhook hook 'orders'" in out
    assert "authorization: Bearer tok" in out
    assert "curl" in out


def test_hooks_delete(monkeypatch, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen["path"] = path
        return {"ok": True, "hook_id": "orders", "kind": "telegram"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.hooks_delete("https://api.example.test", "orders", kind="telegram") == 0
    assert seen["path"] == "/api/agent/hook-triggers?name=orders&kind=telegram"
    out, _ = capsys.readouterr()
    assert "Deleted telegram trigger 'orders'" in out


def test_main_hooks_parsing(monkeypatch):
    seen = {}

    monkeypatch.setattr(commands, "hooks_save",
                        lambda api_url, path, debug=False, sync_response=False:
                        seen.update(file=path) or 0)
    assert main.main(["hooks", "save", "/tmp/hook.json"]) == 0
    assert seen["file"] == "/tmp/hook.json"

    monkeypatch.setattr(commands, "hooks_list",
                        lambda api_url, kind=None, debug=False: seen.update(kind=kind) or 0)
    assert main.main(["hooks", "list", "--kind", "telegram"]) == 0
    assert seen["kind"] == "telegram"
    # The youtube hook kind filters like the others (its rows travel in the
    # same hook-triggers table).
    assert main.main(["hooks", "list", "--kind", "youtube"]) == 0
    assert seen["kind"] == "youtube"

    monkeypatch.setattr(commands, "hooks_delete",
                        lambda api_url, name, kind=None, debug=False:
                        seen.update(deleted=name, delete_kind=kind) or 0)
    assert main.main(["hooks", "delete", "orders"]) == 0
    assert seen["deleted"] == "orders" and seen["delete_kind"] is None
    assert main.main(["hooks", "delete", "orders", "--kind", "youtube"]) == 0
    assert seen["delete_kind"] == "youtube"


def test_connections_import_token_provider_uses_token_file(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"connection_id": "tg-bot", "account_title": "@dapier_bot"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    token_file = tmp_path / "bot-token"
    token_file.write_text("123456:AAAtokentokentokentokentoken\n")

    code = commands.connections_import(
        "https://api.example.test", "tg-bot", "telegram", None, None, None,
        debug=False, token_path=str(token_file))
    assert code == 0
    assert seen["path"] == "/api/agent/connections/import"
    assert seen["body"]["token"] == "123456:AAAtokentokentokentokentoken"
    assert "display_name" not in seen["body"]
    assert "authorized_user" not in seen["body"]
    out, _ = capsys.readouterr()
    assert "@dapier_bot" in out and "123456" not in out

    # A token provider without --token-file is a usage error, not a request.
    assert commands.connections_import(
        "https://api.example.test", "tg-bot", "telegram", None, None, None) == 2


def test_connections_import_passes_root_path(monkeypatch, tmp_path):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(body=body)
        return {"connection_id": "dbx-team", "account_title": "Team Dropbox"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    refresh_file = tmp_path / "refresh.json"
    refresh_file.write_text('{"refresh_token": "r"}')

    code = commands.connections_import(
        "https://api.example.test", "dbx-team", "dropbox", None, None,
        str(refresh_file), root_path="/incoming")

    assert code == 0
    assert seen["body"]["root_path"] == "/incoming"


def test_connections_create_and_edit_call_operator_api(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"connection_id": "dropbox"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    scopes = ["account_info.read", "files.metadata.read"]
    assert commands.connections_create(
        "https://api.example.test", "dropbox", "dropbox", scopes,
        root_path="/_dtc_paperwork/income-invoices",
    ) == 0
    assert commands.connections_edit(
        "https://api.example.test", "dropbox",
        root_path="/incoming",
    ) == 0
    assert calls == [
        ("PUT", "/api/agent/connections", {
            "connection_id": "dropbox", "provider": "dropbox", "scopes": scopes,
            "root_path": "/_dtc_paperwork/income-invoices",
        }),
        ("PUT", "/api/agent/connections/dropbox", {
            "root_path": "/incoming",
        }),
    ]
    out, _ = capsys.readouterr()
    assert "Created dropbox" in out and "Updated dropbox" in out


def test_oauth_clients_set_posts_secret_without_echoing(monkeypatch, tmp_path, capsys):
    posted = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        posted.update(method=method, path=path, body=body)
        return {"provider": "google", "client_id": "g-id", "source": "config", "configured": True}

    monkeypatch.setattr(commands.api, "call", fake_call)
    secret = tmp_path / "g-secret"
    secret.write_text("g-secret-value\n")
    assert commands.oauth_clients_set("https://api.example.test", "youtube", "g-id", str(secret)) == 0
    assert posted == {"method": "PUT", "path": "/api/agent/oauth-clients/youtube",
                      "body": {"client_id": "g-id", "client_secret": "g-secret-value"}}
    out, _ = capsys.readouterr()
    assert "g-secret-value" not in out


def test_oauth_clients_set_reads_stdin_and_rejects_empty_or_missing(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append({"path": path, "body": body})
        return {"provider": "google", "client_id": "g-id", "source": "config", "configured": True}

    monkeypatch.setattr(commands.api, "call", fake_call)
    monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret\n"))
    assert commands.oauth_clients_set("https://api.example.test", "google", "g-id", "-") == 0
    assert calls == [{"path": "/api/agent/oauth-clients/google",
                      "body": {"client_id": "g-id", "client_secret": "s3cret"}}]

    monkeypatch.setattr(sys, "stdin", io.StringIO("ignored\n"))
    monkeypatch.setattr(sys, "stdin", io.StringIO("ignored\n"))
    assert commands.oauth_clients_set("https://api.example.test", "google", "  ", "-") == 2
    assert commands.oauth_clients_set("https://api.example.test", "google", "g-id",
                                      str(tmp_path / "missing")) == 2
    assert len(calls) == 1
    out, _ = capsys.readouterr()
    assert "s3cret" not in out


def test_oauth_clients_list_prints_clients_without_secrets(monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert path == "/api/agent/oauth-clients"
        return {"clients": [
            {"provider": "dropbox", "client_id": "d-id", "source": "config", "configured": True},
            {"provider": "google", "client_id": "", "source": "none", "configured": False},
        ]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.oauth_clients_list("https://api.example.test") == 0
    out, _ = capsys.readouterr()
    assert "dropbox" in out and "d-id" in out
    assert "google" in out and "none" in out


def test_main_oauth_clients_parsing(monkeypatch):
    seen = {}

    def fake_set(api_url, provider, client_id, secret_path, debug=False):
        seen.update(provider=provider, client_id=client_id, secret_path=secret_path)
        return 0

    monkeypatch.setattr(commands, "oauth_clients_set", fake_set)
    assert main.main(["oauth-clients", "set", "google", "--client-id", "g-id",
                      "--client-secret-file", "/tmp/s"]) == 0
    assert seen == {"provider": "google", "client_id": "g-id", "secret_path": "/tmp/s"}

    monkeypatch.setattr(commands, "oauth_clients_list", lambda api_url, debug=False: 0)
    assert main.main(["oauth-clients", "list"]) == 0


# --- dapier tokens: operator API-token management ---

TOKEN_CREATED = {
    "token_id": "personal-scheduler",
    "token_prefix": "dap_abc12345678",
    "agent": "personal-scheduler",
    "subject": "token:personal-scheduler",
    "created_by": "op-1",
    "created_at": "2026-09-24T20:00:00+00:00",
    "token": "dap_SECRET_VALUE",
}


def test_tokens_create_prints_value_once(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return dict(TOKEN_CREATED)

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["tokens", "create", "--name", "personal-scheduler",
                    "--agent", "personal-scheduler"])

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [("PUT", "/api/agent/tokens",
                      {"token_id": "personal-scheduler", "agent": "personal-scheduler"})]
    assert "dap_SECRET_VALUE" in out
    assert "token:personal-scheduler" in out


def test_tokens_create_writes_owner_only_file(isolated_home, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(commands.api, "call", lambda *args, **kwargs: dict(TOKEN_CREATED))
    path = tmp_path / "private" / "host.token"
    assert main.main(["tokens", "create", "--name", "host-worker", "--agent",
                      "host-worker", "--output", str(path)]) == 0
    assert path.read_text().strip() == "dap_SECRET_VALUE"
    from src.dapier.private_files import require_private
    require_private(path)
    assert "SECRET_VALUE" not in capsys.readouterr().out


def test_tokens_list_never_prints_secrets(isolated_home, monkeypatch, capsys):
    listed = {key: value for key, value in TOKEN_CREATED.items() if key != "token"}

    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/tokens")
        return {"tokens": [listed]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["tokens", "list"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "personal-scheduler" in out
    assert "SECRET" not in out


def test_tokens_revoke(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"token_id": "personal-scheduler", "revoked_at": "2026-09-24T21:00:00+00:00"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["tokens", "revoke", "personal-scheduler"])

    assert rc == 0
    assert calls == [("DELETE", "/api/agent/tokens?token_id=personal-scheduler")]


def test_tokens_delete_purges_revoked_token(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"token_id": "personal-scheduler", "grants_removed": 2}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["tokens", "delete", "personal-scheduler"])

    assert rc == 0
    assert calls == [("DELETE", "/api/agent/tokens?token_id=personal-scheduler&purge=1")]
    assert "Removed revoked API token personal-scheduler and 2 connection grants" \
        in capsys.readouterr().out


def test_tokens_empty_list(isolated_home, monkeypatch, capsys):
    monkeypatch.setattr(commands.api, "call",
                        lambda api_url, method, path, body=None, **kwargs: {"tokens": []})

    rc = main.main(["tokens", "list"])

    assert rc == 0
    assert "No API tokens" in capsys.readouterr().out


RUN_STEPS = {
    "run": {"run_id": "wf-1:evt-1", "workflow_id": "wf-1", "status": "failed",
            "connector": "email", "event_type": "message.received", "steps": 2,
            "started_at": "2026-09-25T10:00:00+00:00", "failed_step": "post",
            "error": "Slack rejected message"},
    "steps": [
        {"execution_id": "wf-1:post:evt-1", "action_id": "post", "action_type": "webhook",
         "status": "completed", "duration_ms": 980,
         "input": {"subject": "invoice"}, "output": {"status": 200}},
        {"execution_id": "wf-1:notify:evt-1", "action_id": "notify", "action_type": "slack",
         "status": "failed", "duration_ms": 340, "input": {"subject": "invoice"},
         "error": "Slack rejected message"},
    ],
}


def test_runs_list_hits_agent_endpoint(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"runs": [{"run_id": "wf-1:evt-1", "workflow_id": "wf-1", "status": "failed",
                          "steps": 2, "started_at": "2026-09-25T10:00:00+00:00"}]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "list"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs?limit=25")]
    out = capsys.readouterr().out
    assert "wf-1:evt-1" in out and "failed" in out


def test_runs_show_prints_the_step_flow(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return RUN_STEPS

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "show", "wf-1:evt-1"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs/wf-1%3Aevt-1")]
    out = capsys.readouterr().out
    assert "step[1]: post (webhook)  completed  980ms" in out
    assert "step[2]: notify (slack)  failed  340ms" in out
    assert "error: Slack rejected message" in out
    assert '{"status": 200}' in out


def test_runs_replay_hits_the_agent_replay_endpoint(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"accepted": True, "replayed_from": "wf-1:evt-1",
                "event_id": "replay-abc", "run_id": "wf-1:replay-abc"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "replay", "wf-1:evt-1"])

    assert rc == 0
    assert calls == [("POST", "/api/agent/runs/wf-1%3Aevt-1/replay")]
    out = capsys.readouterr().out
    assert "wf-1:evt-1" in out
    assert "wf-1:replay-abc" in out


def test_runs_replay_from_step_forwards_the_step(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"accepted": True, "replayed_from": "wf-1:evt-1", "from_step": "post",
                "event_id": "replay-abc", "run_id": "wf-1:replay-abc"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "replay", "wf-1:evt-1", "--from-step", "post"])

    assert rc == 0
    assert calls == [("POST", "/api/agent/runs/wf-1%3Aevt-1/replay", {"from_step": "post"})]
    out = capsys.readouterr().out
    assert "from step 'post'" in out
    assert "Earlier steps do not run again" in out


def test_usage_hits_the_agent_endpoint(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"usage": [{"month": "202609", "workflow_id": "wf-1", "tasks": 7}]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["usage"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/usage?months=12")]
    out = capsys.readouterr().out
    assert "wf-1" in out and "202609" in out


def test_runs_list_forwards_filter_flags(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"runs": []}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "list", "--workflow", "wf-1", "--status", "failed",
                    "--since", "2026-09-26T00:00:00+00:00", "--search", "order-1234"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs?limit=25&workflow_id=wf-1&status=failed"
                              "&since=2026-09-26T00%3A00%3A00%2B00%3A00&q=order-1234")]


def test_runs_export_writes_the_csv_file(isolated_home, monkeypatch, tmp_path, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"filename": "dapier-runs-20260925-100000.csv", "count": 1,
                "truncated": False, "csv": "run_id,workflow_id\r\nwf-1:evt-1,wf-1\r\n"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    out_path = tmp_path / "runs.csv"

    rc = main.main(["runs", "export", "--out", str(out_path),
                    "--workflow", "wf-1", "--max-rows", "100"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs/export?workflow_id=wf-1&max_rows=100")]
    assert out_path.read_bytes().endswith(b"wf-1:evt-1,wf-1\r\n")
    assert "1 runs" in capsys.readouterr().out


def test_runs_export_defaults_to_the_suggested_filename(isolated_home, monkeypatch,
                                                        tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"filename": "dapier-runs-20260925-100000.csv", "count": 0,
                "truncated": False, "csv": "run_id,workflow_id\r\n"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "export"])

    assert rc == 0
    assert (tmp_path / "dapier-runs-20260925-100000.csv").exists()


@pytest.mark.parametrize("prefix", [["inbox"], ["runs", "events"]])
def test_inbox_list_hits_agent_endpoint(prefix, isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"events": [{"inbox_id": "evt-1", "connector": "webhook", "status": "unmatched",
                            "received_at": "2026-09-27T10:00:00+00:00"}], "total": 1}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(prefix + ["list", "--connector", "webhook"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/triggers/inbox?limit=25&connector=webhook")]
    out = capsys.readouterr().out
    assert "evt-1" in out and "no workflow" in out


@pytest.mark.parametrize("prefix", [["inbox"], ["runs", "events"]])
def test_inbox_show_prints_the_stored_envelope(prefix, isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/triggers/inbox/evt-1")
        return {"event": {"inbox_id": "evt-1", "connector": "webhook", "status": "unmatched",
                          "matched": [], "data": {"action": "opened"}}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(prefix + ["show", "evt-1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "inbox event: evt-1" in out
    assert "status: unmatched" in out
    assert '{"action": "opened"}' in out


@pytest.mark.parametrize("prefix", [["inbox"], ["runs", "events"]])
def test_inbox_replay_posts_to_the_agent_endpoint(prefix, isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("POST", "/api/agent/triggers/inbox/evt-1/replay")
        return {"accepted": True, "replayed_from": "evt-1", "event_id": "inbox-replay-1",
                "run_id": "webhook:inbox-replay-1"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(prefix + ["replay", "evt-1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "Replay accepted for evt-1" in out


def test_errors_hits_the_agent_endpoint(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"window_days": 7, "total_failed_runs": 2, "workflows": [
            {"workflow_id": "wf-1", "failed_runs": 2,
             "last_failed_at": "2026-09-26T10:00:00+00:00",
             "last_error": "connection timeout"},
        ]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["errors"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/errors/summary?days=7")]
    out = capsys.readouterr().out
    assert "wf-1" in out and "2" in out and "connection timeout" in out


def test_errors_days_flag_reaches_the_endpoint(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"window_days": 30, "total_failed_runs": 0, "workflows": []}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["errors", "--days", "30"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/errors/summary?days=30")]
    assert "No failed runs" in capsys.readouterr().out


def test_runs_cancel_hits_the_agent_cancel_endpoint(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"accepted": True, "run_id": "wf-1:evt-1", "cancelled": 1,
                "run": {"run_id": "wf-1:evt-1", "status": "cancelled"}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "cancel", "wf-1:evt-1"])

    assert rc == 0
    assert calls == [("POST", "/api/agent/runs/wf-1%3Aevt-1/cancel")]
    out = capsys.readouterr().out
    assert "wf-1:evt-1" in out
    assert "will not fire" in out


def test_runs_cancel_of_a_run_not_suspended_reports_the_409(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        raise ApiError("Run is not suspended; nothing to cancel", status=409)

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "cancel", "wf-1:evt-1"])

    assert rc == 5
    assert "Run is not suspended; nothing to cancel" in capsys.readouterr().out


def test_runs_list_warns_when_the_search_window_was_clipped(isolated_home, monkeypatch, capsys):
    """An empty failures list under a clipped window means "none where we
    looked" - the one answer that must never read as a clean bill of health."""
    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"runs": [], "paging": {"bounded": True, "filtered": True}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "list", "--unresolved"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "No runs match these filters." in out
    assert "search window was clipped" in out


def test_runs_list_notes_the_clip_alongside_rows(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"runs": [{"run_id": "wf-1:evt-1", "status": "failed", "steps": 1,
                          "started_at": "2026-09-25T10:00:00+00:00"}],
                "paging": {"bounded": True, "filtered": True}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["runs", "list", "--unresolved"]) == 0
    assert "search window was clipped" in capsys.readouterr().out


def test_runs_list_is_quiet_when_the_window_was_not_clipped(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"runs": [{"run_id": "wf-1:evt-1", "status": "failed", "steps": 1,
                          "started_at": "2026-09-25T10:00:00+00:00"}],
                "paging": {"bounded": False, "filtered": True}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["runs", "list", "--unresolved"]) == 0
    assert "clipped" not in capsys.readouterr().out


def test_runs_resolve_hits_the_agent_resolve_endpoint(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"resolved": True, "already_resolved": False, "run_id": "wf-1:evt-1",
                "steps": 3, "resolved_at": "2026-09-25T12:00:00+00:00",
                "run": {"run_id": "wf-1:evt-1", "status": "failed", "resolved": True}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "resolve", "wf-1:evt-1"])

    assert rc == 0
    assert calls == [("POST", "/api/agent/runs/wf-1%3Aevt-1/resolve", {})]
    out = capsys.readouterr().out
    assert "Marked wf-1:evt-1 fixed" in out
    assert "no longer counts as a problem" in out


def test_runs_resolve_forwards_the_note(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append(body)
        return {"resolved": True, "already_resolved": False, "steps": 1}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["runs", "resolve", "wf-1:evt-1", "--note", "dead workflow"]) == 0
    assert calls == [{"note": "dead workflow"}]


def test_runs_resolve_of_an_already_fixed_run_says_so(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"resolved": True, "already_resolved": True,
                "resolved_reason": "recovered", "resolved_at": "2026-09-26T10:00:00+00:00"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "resolve", "wf-1:evt-1"])

    assert rc == 0
    assert "already fixed (recovered" in capsys.readouterr().out


def test_runs_list_resolved_and_unresolved_map_to_the_api_status(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append(path)
        return {"runs": []}

    monkeypatch.setattr(commands.api, "call", fake_call)

    main.main(["runs", "list", "--unresolved"])
    main.main(["runs", "list", "--resolved"])
    main.main(["runs", "list", "--unresolved", "--workflow", "wf-1"])
    main.main(["runs", "list"])

    assert "status=problems" in calls[0]
    assert "status=resolved" in calls[1]
    assert "status=problems" in calls[2] and "workflow_id=wf-1" in calls[2]
    assert "status=" not in calls[3]  # no filter flag, no status


def test_runs_list_marks_a_fixed_failure_in_the_output(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"runs": [{"run_id": "wf-1:evt-1", "status": "failed", "steps": 1,
                          "started_at": "2026-09-25T10:00:00+00:00",
                          "resolved": True, "resolved_reason": "recovered"}]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["runs", "list"]) == 0
    assert "failed (fixed: recovered)" in capsys.readouterr().out


def test_runs_show_prints_the_resolution(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"run": {"run_id": "wf-1:evt-1", "status": "failed", "steps": 1,
                        "resolved": True, "resolved_reason": "acknowledged",
                        "resolved_at": "2026-09-25T12:00:00+00:00",
                        "resolved_by": "op-1"},
                "steps": [{"action_id": "post", "status": "failed"}]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert main.main(["runs", "show", "wf-1:evt-1"]) == 0
    out = capsys.readouterr().out
    assert "resolved: acknowledged at 2026-09-25T12:00:00+00:00 by op-1" in out


# --- Audit trail, workflows export, and server-side workflow search ---

def test_audit_hits_the_agent_endpoint(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"events": [{
            "connection_id": "youtube-personal", "action": "connect",
            "actor_subject": "op-1", "agent": None, "outcome": "created",
            "timestamp": "2026-09-28T10:00:00+00:00",
        }]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["audit", "list", "--action", "connect", "--limit", "10"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/audit?limit=10&action=connect")]
    out = capsys.readouterr().out
    assert "youtube-personal" in out and "connect" in out and "created" in out


def test_audit_empty_trail_prints_a_hint(isolated_home, monkeypatch, capsys):
    monkeypatch.setattr(commands.api, "call",
                        lambda *args, **kwargs: {"events": []})

    rc = main.main(["audit", "list"])

    assert rc == 0
    assert "No audit events" in capsys.readouterr().out


def test_workflows_export_prints_the_canonical_yaml(isolated_home, monkeypatch, capsys):
    def fake_call(api_url, method, path, body=None, **kwargs):
        assert (method, path) == ("GET", "/api/agent/designer/workflows/test-flow.yaml")
        return {"workflow": {"id": "test-flow"}, "published": False,
                "yaml": "id: test-flow\nactions:\n- type: webhook\n"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["workflows", "export", "test-flow.yaml"])

    assert rc == 0
    assert "id: test-flow" in capsys.readouterr().out


def test_workflows_export_writes_the_output_file_verbatim(isolated_home, monkeypatch, tmp_path, capsys):
    yaml_text = "id: test-flow\nactions:\n- type: webhook\n"
    monkeypatch.setattr(commands.api, "call",
                        lambda *args, **kwargs: {"workflow": {"id": "test-flow"}, "yaml": yaml_text})
    target = tmp_path / "export.yaml"

    rc = main.main(["workflows", "export", "test-flow.yaml", "-o", str(target)])

    assert rc == 0
    assert target.read_text() == yaml_text
    assert f"Exported test-flow.yaml to {target}" in capsys.readouterr().out


def _server_bundle_bytes(names=("invoice-alert.yaml", "standup-digest.yaml")):
    """A server-shaped export bundle: manifest.json plus workflows/<file>."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps(
            {"count": len(names),
             "workflows": [{"file": name} for name in names]}))
        for name in names:
            bundle.writestr(f"workflows/{name}", f"id: {name.removesuffix('.yaml')}\n")
    return buffer.getvalue()


def test_workflows_export_all_writes_the_server_bundle(isolated_home, monkeypatch,
                                                       tmp_path, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"filename": "dapier-workflows-20260928.zip", "count": 2,
                "skipped": ["odd"],
                "b64": base64.b64encode(_server_bundle_bytes()).decode()}

    monkeypatch.setattr(commands.api, "call", fake_call)
    target = tmp_path / "bundle.zip"

    rc = main.main(["workflows", "export", "--all", "-o", str(target)])

    assert rc == 0
    assert calls == [("GET", "/api/agent/designer/export")]
    with zipfile.ZipFile(target) as bundle:
        assert bundle.namelist() == ["manifest.json",
                                     "workflows/invoice-alert.yaml",
                                     "workflows/standup-digest.yaml"]
        assert bundle.read("workflows/standup-digest.yaml").decode() == \
            "id: standup-digest\n"
    out = capsys.readouterr().out
    assert "Exported 2 workflow(s) to" in out
    assert "skipped: odd" in out


def test_workflows_export_all_defaults_to_the_server_suggested_name(isolated_home,
                                                                    monkeypatch,
                                                                    tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands.api, "call",
                        lambda *args, **kwargs: {
                            "filename": "dapier-workflows-20260928.zip",
                            "count": 1, "skipped": [],
                            "b64": base64.b64encode(
                                _server_bundle_bytes(("solo.yaml",))).decode()})

    rc = main.main(["workflows", "export", "--all"])

    assert rc == 0
    with zipfile.ZipFile(tmp_path / "dapier-workflows-20260928.zip") as bundle:
        assert bundle.namelist() == ["manifest.json", "workflows/solo.yaml"]


def test_workflows_export_all_refuses_an_empty_bundle(isolated_home, monkeypatch,
                                                      tmp_path, capsys):
    monkeypatch.setattr(commands.api, "call",
                        lambda *args, **kwargs: {"filename": "dapier-workflows-x.zip",
                                                 "count": 0, "skipped": [],
                                                 "b64": base64.b64encode(
                                                     _server_bundle_bytes(())).decode()})
    target = tmp_path / "empty.zip"

    rc = main.main(["workflows", "export", "--all", "-o", str(target)])

    assert rc == 2
    assert "No workflows to export" in capsys.readouterr().out
    assert not target.exists()


def test_workflows_export_all_reports_an_unreadable_bundle(isolated_home, monkeypatch,
                                                           tmp_path, capsys):
    monkeypatch.setattr(commands.api, "call",
                        lambda *args, **kwargs: {"filename": "dapier-workflows-x.zip",
                                                 "count": 2, "skipped": [],
                                                 "b64": "zzz"})
    target = tmp_path / "broken.zip"

    rc = main.main(["workflows", "export", "--all", "-o", str(target)])

    assert rc == 5
    assert "unreadable bundle" in capsys.readouterr().out
    assert not target.exists()


def test_workflows_list_forwards_the_search(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"workflows": [{"id": "invoice-alert", "enabled": True,
                               "source": "invoice-alert.yaml", "connector": "email",
                               "event": "message.received", "actionCount": 1}],
                "git_sync": {"configured": False, "repo": "r", "branch": "main"}}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["workflows", "list", "--search", "invoice"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/designer/workflows?q=invoice")]
    assert "invoice-alert" in capsys.readouterr().out


def test_connections_list_prints_health_and_expiry(capsys, monkeypatch):
    def fake_call(url, method, path, body=None, debug=False):
        assert path == "/api/agent/connections"
        return {"connections": [
            {"connection_id": "youtube-personal", "provider": "youtube", "status": "connected",
             "health": "expired", "token_expires_at": "2026-01-02T03:04:00+00:00",
             "account_title": "Ch",
             "services": [{"id": "youtube", "label": "YouTube"}]},
            {"connection_id": "google-calendar", "provider": "google", "status": "connected",
             "health": "ok", "account_title": "ops@example.test",
             "services": [
                 {"id": "gmail", "label": "Gmail"},
                 {"id": "calendar", "label": "Google Calendar"},
             ]},
            {"connection_id": "slack-team", "provider": "slack", "status": "connected",
             "health": "ok"},
        ]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_list("https://api.example.test") == 0
    out = capsys.readouterr().out
    assert "HEALTH" in out and "EXPIRES" in out
    assert "SERVICES" in out
    assert "YouTube" in out
    assert "Gmail, Google Calendar" in out
    assert "expired" in out
    assert "2026-01-02" in out
    assert " ok " in out


def test_zoom_connections_show_explains_each_type(capsys, monkeypatch):
    from src.dapier.connections.records import public_view

    for scopes, label, purpose in (
        (["user:read:user"], "Zoom API", "permissions granted"),
        ([], "Zoom Webhooks", "event notifications"),
    ):
        view = public_view({"connection_id": "example", "provider": "zoom", "scopes": scopes})
        monkeypatch.setattr(commands.api, "call", lambda *args, **kwargs: view)
        assert commands.connections_show("https://api.example.test", "example") == 0
        out = capsys.readouterr().out
        assert label in out and purpose in out


def test_connections_show_prints_health(capsys, monkeypatch):
    def fake_call(url, method, path, body=None, debug=False):
        assert path == "/api/agent/connections/youtube-personal"
        return {"connection_id": "youtube-personal", "provider": "youtube",
                "status": "connected", "health": "expired",
                "token_expires_at": "2026-01-02T03:04:00+00:00"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_show("https://api.example.test", "youtube-personal") == 0
    out = capsys.readouterr().out
    assert "health: expired" in out
    assert "token_expires_at: 2026-01-02" in out


def test_connections_show_names_the_account_not_a_display_name(capsys, monkeypatch):
    def fake_call(url, method, path, body=None, debug=False):
        return {
            "connection_id": "google-sheets",
            "provider": "google",
            "display_name": "Google Drive, Docs & Sheets (DataTalks)",
            "status": "connected",
            "account_title": "alexey@datatalks.club",
            "verified_account_id": "alexey@datatalks.club",
            "services": [
                {"id": "drive", "label": "Google Drive"},
                {"id": "docs", "label": "Google Docs"},
                {"id": "sheets", "label": "Google Sheets"},
            ],
        }

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_show("https://api.example.test", "google-sheets") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "account: alexey@datatalks.club"
    # A leftover stored display_name is never printed: account + service name it.
    assert not any("Google Drive, Docs & Sheets (DataTalks)" in line for line in lines)


def test_cli_has_no_display_name_flag():
    for argv in (["connections", "create", "--provider", "dropbox", "--scopes", "a",
                  "--display-name", "X"],
                 ["connections", "edit", "dropbox", "--display-name", "X"],
                 ["connections", "import", "tg", "--provider", "telegram",
                  "--display-name", "X"]):
        with pytest.raises(SystemExit):
            main.build_parser().parse_args(argv)


def test_connections_list_and_show_say_who_a_slack_token_acts_as(capsys, monkeypatch):
    rows = [
        {"connection_id": "slack", "provider": "slack", "status": "connected",
         "account_title": "DataTalks.Club", "verified_account_id": "T1",
         "account_identity": {"kind": "app", "name": "Au-Tomator", "label": "Au-Tomator (App)"}},
        {"connection_id": "slack-2", "provider": "slack", "status": "connected",
         "account_title": "DataTalks.Club", "verified_account_id": "T1",
         "account_identity": {"kind": "user", "name": "Alexey Grigorev",
                              "label": "Alexey Grigorev (User)"}},
        {"connection_id": "zoom", "provider": "zoom", "status": "ready", "scopes": []},
    ]
    commands.print_connections(rows)
    out = capsys.readouterr().out
    assert "DataTalks.Club · Au-Tomator (App)" in out
    assert "DataTalks.Club · Alexey Grigorev (User)" in out
    assert "Zoom webhook" in out
    commands.print_connection(rows[1])
    assert capsys.readouterr().out.splitlines()[0] == "account: DataTalks.Club · Alexey Grigorev (User)"


def test_templates_command_is_removed():
    with pytest.raises(SystemExit) as exc:
        main.main(["templates", "list"])
    assert exc.value.code == 2


def test_catalog_lists_each_event_with_its_description(monkeypatch, capsys):
    catalog = {"actions": [], "connectors": [
        {"name": "custom", "label": "Custom", "events": [], "event_info": []},
        {"name": "youtube", "label": "YouTube", "events": ["video.published"],
         "event_info": [{"event": "video.published", "label": "Video published",
                         "description": "A new video goes live on the channel"}]},
    ]}
    monkeypatch.setattr(commands.api, "call", lambda *args, **kwargs: catalog)
    assert main.main(["catalog"]) == 0
    out = capsys.readouterr().out
    assert "(any event)" in out
    assert "video.published" in out
    assert "Video published — A new video goes live on the channel" in out

