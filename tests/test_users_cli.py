"""`dapier users` CLI coverage: a thin client over /api/agent/users.

The API behavior lives in tests/test_roles.py; here we pin the command
surface — the exact endpoints, the table rendering, the remove confirmation,
and the exit codes the main() dispatcher maps ApiError statuses to.
"""
import json

import pytest

from dapier_cli import commands, main
from dapier_cli.api import ApiError


USER_ROW = {"subject": "dev@example.test", "role": "editor",
            "display_name": "Dev", "updated_at": "2026-09-28T00:00:00+00:00",
            "updated_by": "op@example.test"}


def fake_api(monkeypatch, calls, users=None, response=None):
    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append({"method": method, "path": path, "body": body})
        if method == "GET":
            return {"users": list(users or [])}
        return dict(response or {"ok": True})
    monkeypatch.setattr(commands.api, "call", fake_call)


def test_users_list_prints_the_table(monkeypatch, capsys):
    calls = []
    fake_api(monkeypatch, calls, users=[USER_ROW])
    assert commands.users_list("https://api.example.test") == 0
    assert calls == [{"method": "GET", "path": "/api/agent/users", "body": None}]
    out = capsys.readouterr().out
    assert "dev@example.test" in out
    assert "editor" in out
    assert "Dev" in out
    assert "active" in out


def test_users_list_marks_disabled_users(monkeypatch, capsys):
    calls = []
    fake_api(monkeypatch, calls, users=[dict(USER_ROW, disabled=True)])
    assert commands.users_list("https://api.example.test") == 0
    assert "disabled" in capsys.readouterr().out


def test_users_list_empty_store_hints_at_the_allowlist(monkeypatch, capsys):
    calls = []
    fake_api(monkeypatch, calls, users=[])
    assert commands.users_list("https://api.example.test") == 0
    out = capsys.readouterr().out
    assert "allowlist" in out
    assert "`dapier users set-role`" in out


def test_users_set_role_posts_subject_role_and_display_name(monkeypatch, capsys):
    calls = []
    fake_api(monkeypatch, calls, response=dict(USER_ROW))
    assert commands.users_set_role("https://api.example.test", "dev@example.test",
                                   "editor", display_name="Dev") == 0
    assert calls == [{"method": "POST", "path": "/api/agent/users",
                      "body": {"subject": "dev@example.test", "role": "editor",
                               "display_name": "Dev"}}]
    assert "dev@example.test" in capsys.readouterr().out


def test_users_set_role_without_display_name_sends_the_pair(monkeypatch):
    calls = []
    fake_api(monkeypatch, calls, response=dict(USER_ROW))
    assert commands.users_set_role("https://api.example.test", "user@example.test",
                                   "viewer") == 0
    assert calls[0]["body"] == {"subject": "user@example.test", "role": "viewer"}


def test_users_remove_yes_skips_the_prompt(monkeypatch):
    calls = []
    fake_api(monkeypatch, calls)
    assert commands.users_remove("https://api.example.test", "dev@example.test",
                                 assume_yes=True) == 0
    assert calls == [{"method": "DELETE",
                      "path": "/api/agent/users?subject=dev%40example.test",
                      "body": None}]


def test_users_remove_prompts_and_respects_the_answer(monkeypatch):
    calls = []
    fake_api(monkeypatch, calls)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert commands.users_remove("https://api.example.test", "dev@example.test") == 0
    assert len(calls) == 1
    monkeypatch.setattr("builtins.input", lambda prompt: "no\n")
    assert commands.users_remove("https://api.example.test", "dev@example.test") == 1
    assert len(calls) == 1  # declined: no DELETE was sent


def test_users_remove_without_a_terminal_never_deletes(monkeypatch, capsys):
    calls = []
    fake_api(monkeypatch, calls)

    def boom(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", boom)
    assert commands.users_remove("https://api.example.test", "dev@example.test") == 2
    assert calls == []
    assert "--yes" in capsys.readouterr().out


def test_users_main_dispatch(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    seen = []

    def fake_users_list(api_url, debug=False):
        seen.append(("list", api_url))
        return 0

    def fake_set(api_url, subject, role, display_name=None, debug=False):
        seen.append(("set-role", subject, role, display_name))
        return 0

    def fake_remove(api_url, subject, assume_yes=False, debug=False):
        seen.append(("remove", subject, assume_yes))
        return 0

    monkeypatch.setattr(commands, "users_list", fake_users_list)
    monkeypatch.setattr(commands, "users_set_role", fake_set)
    monkeypatch.setattr(commands, "users_remove", fake_remove)
    assert main.main(["users", "list"]) == 0
    assert main.main(["users", "set-role", "dev@example.test", "admin"]) == 0
    assert main.main(["users", "remove", "dev@example.test", "--yes"]) == 0
    default_url = "https://dapier.dtcdev.click"
    assert seen == [
        ("list", default_url),
        ("set-role", "dev@example.test", "admin", None),
        ("remove", "dev@example.test", True),
    ]


def test_users_rejects_unknown_roles_at_parse_time():
    with pytest.raises(SystemExit):
        main.build_parser().parse_args(
            ["users", "set-role", "dev@example.test", "owner"])


def test_users_denied_maps_to_exit_code_4(monkeypatch, capsys):
    def forbidden(api_url, method, path, body=None, **kwargs):
        raise ApiError("This action needs the 'admin' role", status=403)

    monkeypatch.setattr(commands.api, "call", forbidden)
    assert main.main(["users", "list"]) == 4
    assert "admin" in capsys.readouterr().out
