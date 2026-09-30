"""CLI surface for all four trigger kinds: email, hook, schedule, poll.

Every command is a thin client over the operator-gated /api/agent/*-triggers
routes (the tests stub commands.api.call, which is also the assertion that
the commands go through the shared session-authenticated client rather than
bypassing the API).
"""

import json

from dapier_cli import commands, main
from dapier_cli.api import ApiError


TRIGGER = {
    "name": "consulting",
    "address": "consulting@dtcdev.click",
    "enabled": True,
    "actions": [{"type": "dropbox_upload", "connection_id": "dropbox"}],
}

HOOK = {
    "hook_id": "orders",
    "kind": "webhook",
    "url": "https://dapier.example.test/hooks/webhook/orders",
    "token": "hook-token-value",
    "header": "authorization",
    "enabled": True,
    "actions": [{"type": "webhook", "url": "https://hooks.test/x"}],
}

SCHEDULE = {
    "schedule_id": "morning-digest",
    "expression": "cron(0 7 * * ? *)",
    "rule": "dapier-schedule-morning-digest",
    "enabled": True,
    "flow": "morning-digest",
}

POLL = {
    "poll_id": "inbox-watch",
    "expression": "rate(15 minutes)",
    "url": "https://api.example.test/items",
    "method": "GET",
    "rule": "dapier-poll-inbox-watch",
    "enabled": True,
}


def test_list_renders_all_four_kinds(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        if path == "/api/agent/email-triggers":
            return {"domain": "dtcdev.click", "triggers": [TRIGGER], "managed_routes": []}
        if path == "/api/agent/hook-triggers":
            return {"base_url": "https://dapier.example.test", "hooks": [HOOK], "flows": []}
        if path == "/api/agent/schedule-triggers":
            return {"schedules": [SCHEDULE], "flows": []}
        return {"polls": [POLL], "flows": []}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.triggers_list("https://api.example.test") == 0
    assert commands.hooks_list("https://api.example.test") == 0
    assert commands.schedules_list("https://api.example.test") == 0
    assert commands.polls_list("https://api.example.test") == 0
    assert calls == [
        ("GET", "/api/agent/email-triggers"),
        ("GET", "/api/agent/hook-triggers"),
        ("GET", "/api/agent/schedule-triggers"),
        ("GET", "/api/agent/poll-triggers"),
    ]
    out, _ = capsys.readouterr()
    assert "consulting@dtcdev.click" in out  # email
    assert "/hooks/webhook/orders" in out  # hook
    assert "morning-digest" in out and "cron(0 7 * * ? *)" in out  # schedule
    assert "inbox-watch" in out and "https://api.example.test/items" in out  # poll


def test_save_puts_each_kind_route(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        if path == "/api/agent/email-triggers":
            return {"created": True, "name": "consulting", "address": TRIGGER["address"]}
        if path == "/api/agent/hook-triggers":
            return {"created": True, "hook_id": "orders", "kind": "webhook",
                    "url": HOOK["url"], "token": "tok", "header": "authorization"}
        if path == "/api/agent/schedule-triggers":
            return {"created": True, "schedule_id": "morning-digest",
                    "expression": SCHEDULE["expression"], "rule": SCHEDULE["rule"]}
        return {"created": True, "poll_id": "inbox-watch", "expression": POLL["expression"],
                "method": "GET", "url": POLL["url"], "rule": POLL["rule"]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    def save(kind, payload):
        path = tmp_path / f"{kind}.json"
        path.write_text(json.dumps(payload))
        savers = {
            "email": commands.triggers_save,
            "hook": commands.hooks_save,
            "schedule": commands.schedules_save,
            "poll": commands.polls_save,
        }
        return savers[kind]("https://api.example.test", str(path))

    assert save("email", {"name": "consulting", "actions": TRIGGER["actions"]}) == 0
    assert (seen["method"], seen["path"]) == ("PUT", "/api/agent/email-triggers")
    assert save("hook", {"kind": "webhook", "name": "orders", "actions": HOOK["actions"]}) == 0
    assert (seen["method"], seen["path"]) == ("PUT", "/api/agent/hook-triggers")
    assert save("schedule", {"name": "morning-digest", "expression": SCHEDULE["expression"]}) == 0
    assert (seen["method"], seen["path"]) == ("PUT", "/api/agent/schedule-triggers")
    assert save("poll", {"name": "inbox-watch", "expression": POLL["expression"],
                         "url": POLL["url"]}) == 0
    assert (seen["method"], seen["path"]) == ("PUT", "/api/agent/poll-triggers")
    out, _ = capsys.readouterr()
    assert "Created webhook hook 'orders'" in out
    assert "Created schedule 'morning-digest'" in out
    assert "Created poll trigger 'inbox-watch'" in out

    # Bad input files fail locally without a request.
    assert commands.schedules_save("https://api.example.test", str(tmp_path / "nope")) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert commands.polls_save("https://api.example.test", str(bad)) == 2


def test_delete_calls_each_kind_route(monkeypatch, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path)
        if path.startswith("/api/agent/email-triggers"):
            return {"ok": True, "address": TRIGGER["address"]}
        if path.startswith("/api/agent/hook-triggers"):
            return {"ok": True, "hook_id": "orders", "kind": "telegram"}
        if path.startswith("/api/agent/schedule-triggers"):
            return {"ok": True, "schedule_id": "morning-digest"}
        return {"ok": True, "poll_id": "inbox-watch"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.triggers_delete("https://api.example.test", "consulting") == 0
    assert (seen["method"], seen["path"]) == ("DELETE", "/api/agent/email-triggers?name=consulting")
    assert commands.hooks_delete("https://api.example.test", "orders", kind="telegram") == 0
    assert (seen["method"], seen["path"]) == ("DELETE", "/api/agent/hook-triggers?name=orders&kind=telegram")
    assert commands.schedules_delete("https://api.example.test", "morning-digest") == 0
    assert (seen["method"], seen["path"]) == ("DELETE", "/api/agent/schedule-triggers?name=morning-digest")
    assert commands.polls_delete("https://api.example.test", "inbox-watch") == 0
    assert (seen["method"], seen["path"]) == ("DELETE", "/api/agent/poll-triggers?name=inbox-watch")
    out, _ = capsys.readouterr()
    assert "Deleted consulting@dtcdev.click" in out
    assert "Deleted telegram trigger 'orders'" in out
    assert "Deleted schedule trigger 'morning-digest'" in out
    assert "Deleted poll trigger 'inbox-watch'" in out


def test_main_dispatch_for_hook_schedule_poll(monkeypatch):
    seen = {}

    monkeypatch.setattr(commands, "schedules_save",
                        lambda api_url, path, debug=False: seen.setdefault("calls", []).append(("schedules", path)) or 0)
    monkeypatch.setattr(commands, "polls_delete",
                        lambda api_url, name, debug=False: seen.setdefault("calls", []).append(("polls", name)) or 0)
    monkeypatch.setattr(commands, "hooks_list",
                        lambda api_url, kind=None, debug=False: seen.setdefault("calls", []).append(("hooks", kind)) or 0)
    assert main.main(["schedules", "save", "/tmp/schedule.json"]) == 0
    assert main.main(["polls", "delete", "inbox-watch"]) == 0
    assert main.main(["hooks", "list"]) == 0
    assert seen["calls"] == [("schedules", "/tmp/schedule.json"), ("polls", "inbox-watch"), ("hooks", None)]


def test_api_errors_surface_the_server_message(monkeypatch, capsys, tmp_path):
    responses = {
        "GET": ApiError("Denied", status=403),
        "PUT": ApiError("expression must be cron(...) or rate(...)", status=400),
        "DELETE": ApiError("no schedule trigger named 'missing'", status=404),
    }

    def fake_call(api_url, method, path, body=None, **kwargs):
        raise responses[method]

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert main.main(["schedules", "list"]) == 4  # 403 shares the 404 exit code
    out, _ = capsys.readouterr()
    assert "Error: Denied" in out

    body_file = tmp_path / "schedule.json"
    body_file.write_text("{}")
    assert main.main(["schedules", "save", str(body_file)]) == 5
    out, _ = capsys.readouterr()
    assert "Error: expression must be cron(...) or rate(...)" in out

    assert main.main(["schedules", "delete", "missing"]) == 4  # 404 maps to its exit code
    out, _ = capsys.readouterr()
    assert "Error: no schedule trigger named 'missing'" in out


def test_no_session_fails_through_the_shared_api_client(monkeypatch, capsys):
    """The CLI carries no operator logic of its own: the shared client's
    session check is what gates the operator routes client-side."""
    from dapier_cli import api as cli_api

    monkeypatch.setattr(cli_api.config, "load_session", lambda: None)
    assert main.main(["hooks", "list"]) == 3
    out, _ = capsys.readouterr()
    assert "Not signed in" in out
