"""Test-before-publish (#10): engine dry-run/execute plus the admin, agent,
and CLI surfaces that drive it."""

import json

import pytest
import yaml

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.engine import dryrun


WORKFLOW = {
    "id": "notify-video",
    "enabled": True,
    "trigger": {"connector": "youtube", "event": "video.published"},
    "actions": [
        {"id": "prep", "type": "dataops", "operation": "noop", "title": "{title}"},
        {"id": "tell", "type": "slack", "channel": "#videos", "text": "New: {title} at {url}"},
    ],
}

SAMPLE = {"title": "Dry-run demo", "url": "https://yt.test/x"}


@pytest.fixture
def no_runners(monkeypatch):
    """Dry-runs must never reach a real connector: every runner explodes."""
    from src.dapier import engine

    def explode(name):
        def boom(*args, **kwargs):
            raise AssertionError(f"dry-run invoked the {name} runner")
        return boom

    for name in ("run_webhook", "run_slack", "run_telegram_send", "run_email_send",
                 "run_dataops", "run_dropbox_upload", "run_dropbox_delete", "run_render_job"):
        monkeypatch.setattr(engine, name, explode(name))


# ---- engine: dry-run ----

def test_dry_run_renders_steps_without_side_effects(no_runners):
    report = dryrun.dry_run(WORKFLOW, SAMPLE)
    assert report["mode"] == "dry-run"
    assert report["matched"] is True
    assert report["ok"] is True
    assert report["event"]["connector"] == "youtube"
    assert report["event"]["data"] == SAMPLE
    assert [step["action_id"] for step in report["steps"]] == ["prep", "tell"]
    assert report["steps"][0]["rendered_input"]["title"] == "Dry-run demo"
    assert report["steps"][1]["rendered_input"]["text"] == "New: Dry-run demo at https://yt.test/x"


def test_dry_run_sample_defaults_to_the_primary_trigger(no_runners):
    report = dryrun.dry_run(WORKFLOW, {"title": "x"})
    assert report["event"]["connector"] == "youtube"
    assert report["event"]["event"] == "video.published"


def test_dry_run_accepts_a_full_envelope(no_runners):
    envelope = {"connector": "email", "event": "message.received",
                "data": {"subject": "hi"}, "id": "evt-1"}
    report = dryrun.dry_run(WORKFLOW, envelope)
    assert report["event"]["connector"] == "email"
    assert report["event"]["data"] == {"subject": "hi"}
    assert report["matched"] is False


def test_dry_run_reports_filter_mismatch(no_runners):
    workflow = {**WORKFLOW,
                "trigger": {"connector": "youtube", "event": "video.published",
                            "filters": {"title": {"prefix": "Invoice"}}}}
    report = dryrun.dry_run(workflow, SAMPLE)
    assert report["matched"] is False
    assert [step["ok"] for step in report["steps"]] == [True, True]


def test_dry_run_flags_unsupported_actions_and_bad_templates(no_runners):
    workflow = {**WORKFLOW, "actions": [
        {"id": "weird", "type": "mystery_action", "x": "{title}"},
        {"id": "broken", "type": "slack", "channel": "#c", "text": "{title.missing}"},
    ]}
    report = dryrun.dry_run(workflow, SAMPLE)
    assert report["ok"] is False
    assert report["steps"][0]["ok"] is False
    assert report["steps"][0]["error"] == "unsupported action: mystery_action"
    assert report["steps"][0]["rendered_input"]["x"] == "Dry-run demo"
    assert report["steps"][1]["ok"] is False
    assert "template error" in report["steps"][1]["error"]


def test_dry_run_resolves_flow_bindings(tmp_path, monkeypatch, no_runners):
    (tmp_path / "flows.yaml").write_text(
        "flows:\n  shared-notify:\n    actions:\n      - {id: hook, type: webhook, url: 'https://x.test/{title}'}\n")
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    workflow = {"id": "flow-user", "trigger": {"connector": "youtube", "event": "video.published"},
                "flow": "shared-notify"}
    report = dryrun.dry_run(workflow, SAMPLE)
    assert report["ok"] is True
    assert report["steps"][0]["rendered_input"]["url"] == "https://x.test/Dry-run demo"


def test_dry_run_undefined_flow_is_an_error(no_runners):
    with pytest.raises(dryrun.TestRunError, match="no shared flow"):
        dryrun.dry_run({**WORKFLOW, "flow": "nope", "actions": None}, SAMPLE)


# ---- engine: execute ----

def test_execute_runs_the_chain_through_the_real_engine(monkeypatch):
    from src.dapier import engine

    calls = []

    def fake_dataops(action, event):
        calls.append(("dataops", action["id"]))
        return {"accepted": True}

    def fake_slack(action, event, steps=None):
        calls.append(("slack", action["channel"], event["data"]["title"]))
        return {"ok": True, "ts": "1"}

    monkeypatch.setattr(engine, "run_dataops", fake_dataops)
    monkeypatch.setattr(engine, "run_slack", fake_slack)
    report = dryrun.test_run(WORKFLOW, SAMPLE, execute=True)
    assert report["mode"] == "execute"
    assert report["ok"] is True
    assert calls == [("dataops", "prep"), ("slack", "#videos", "Dry-run demo")]
    assert report["steps"][-1] == {"action_id": "tell", "action_type": "slack",
                                   "ok": True, "output": {"ok": True, "ts": "1"}}


def test_execute_stops_the_chain_on_a_failed_step(monkeypatch):
    from src.dapier import engine

    ran = []

    def failing(action, event):
        ran.append(action["id"])
        raise RuntimeError("no connection configured")

    def never(action, event):
        raise AssertionError("chain continued past a failed step")

    monkeypatch.setattr(engine, "run_dataops", failing)
    monkeypatch.setattr(engine, "run_slack", never)
    report = dryrun.test_run(WORKFLOW, SAMPLE, execute=True)
    assert ran == ["prep"]
    assert report["ok"] is False
    assert report["steps"][0]["error"] == "no connection configured"
    assert len(report["steps"]) == 1


# ---- admin API ----

def admin_request(method, path, body=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "body": json.dumps(body) if body is not None else None,
    }


@pytest.fixture
def operator_session(monkeypatch):
    from src.dapier.auth import session

    monkeypatch.setattr(session, "authenticated", lambda event: True)
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(session, "require_operator", lambda event: ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    (tmp_path / "notify-video.yaml").write_text(yaml.safe_dump(WORKFLOW))
    return tmp_path


def test_admin_test_requires_a_session(monkeypatch):
    from src.dapier.auth import session

    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test", {"event": {}}),
        "POST", "/api/admin/designer/workflows/test",
    )
    assert response["statusCode"] == 401


def test_admin_test_dry_runs_the_inline_draft(operator_session, no_runners):
    draft = {**WORKFLOW, "id": "brand-new"}
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test",
                      {"event": SAMPLE, "workflow": draft}),
        "POST", "/api/admin/designer/workflows/test",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["file"] == "brand-new.yaml"
    assert payload["mode"] == "dry-run"
    assert payload["matched"] is True
    assert payload["ok"] is True
    assert payload["steps"][1]["rendered_input"]["text"] == "New: Dry-run demo at https://yt.test/x"


def test_admin_test_falls_back_to_the_saved_file(operator_session, bundle, no_runners):
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/notify-video.yaml/test",
                      {"event": SAMPLE}),
        "POST", "/api/admin/designer/workflows/notify-video.yaml/test",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["file"] == "notify-video.yaml"
    assert [step["action_id"] for step in payload["steps"]] == ["prep", "tell"]


def test_admin_test_validates_the_inline_workflow(operator_session, bundle):
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test",
                      {"event": SAMPLE, "workflow": {"id": "x", "actions": []}}),
        "POST", "/api/admin/designer/workflows/test",
    )
    assert response["statusCode"] == 400
    assert "trigger" in json.loads(response["body"])["error"]


def test_admin_test_requires_a_sample_event(operator_session, bundle):
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test", {"workflow": WORKFLOW}),
        "POST", "/api/admin/designer/workflows/test",
    )
    assert response["statusCode"] == 400
    assert "event" in json.loads(response["body"])["error"]


def test_admin_test_unknown_file_is_404(operator_session, bundle):
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/missing.yaml/test", {"event": SAMPLE}),
        "POST", "/api/admin/designer/workflows/missing.yaml/test",
    )
    assert response["statusCode"] == 404


# ---- agent API ----

def agent_request(body=None, token="dtc-id-token"):
    headers = {"host": "dapier.example.test"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = {"headers": headers, "cookies": []}
    if body is not None:
        request["body"] = json.dumps(body)
    return request


@pytest.fixture
def operator_bearer(monkeypatch):
    import boto3

    agent_api.reset_rate_limits()
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: type("D", (), {"Table": lambda self, name: None})())
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "op-1", "email": "op@datatalks.club"},
    )


def test_agent_test_requires_an_operator_bearer(monkeypatch):
    import boto3

    agent_api.reset_rate_limits()
    monkeypatch.setattr(boto3, "resource", lambda service: type("D", (), {"Table": lambda self, name: None})())
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)

    def identity(email):
        return lambda token, audience=None: {"sub": "someone", "email": email}

    anonymous = {"headers": {"host": "dapier.example.test"}, "cookies": [],
                 "body": json.dumps({"event": SAMPLE})}
    assert agent_api.route(anonymous, "POST", "/api/agent/designer/workflows/test")["statusCode"] == 401

    monkeypatch.setattr(agent_api, "verify_id_token", identity("agent@example.test"))
    assert agent_api.route(agent_request({"event": SAMPLE}), "POST",
                           "/api/agent/designer/workflows/test")["statusCode"] == 403


def test_agent_test_dry_run_and_execute_over_bearer(operator_bearer, monkeypatch):
    from src.dapier import engine

    draft = {**WORKFLOW, "id": "cli-draft"}
    dry = agent_api.route(
        agent_request({"event": SAMPLE, "workflow": draft}),
        "POST", "/api/agent/designer/workflows/test",
    )
    assert dry["statusCode"] == 200
    payload = json.loads(dry["body"])
    assert payload["mode"] == "dry-run"
    assert payload["file"] == "cli-draft.yaml"
    assert payload["ok"] is True

    monkeypatch.setattr(engine, "run_dataops", lambda action, event: {"accepted": True})
    monkeypatch.setattr(engine, "run_slack", lambda action, event, steps=None: {"ok": True, "ts": "9"})
    live = agent_api.route(
        agent_request({"event": SAMPLE, "workflow": draft, "execute": True}),
        "POST", "/api/agent/designer/workflows/test",
    )
    assert live["statusCode"] == 200
    executed = json.loads(live["body"])
    assert executed["mode"] == "execute"
    assert executed["ok"] is True
    assert executed["steps"][-1]["output"] == {"ok": True, "ts": "9"}


def test_agent_test_file_fallback_and_bad_body(operator_bearer, bundle, no_runners):
    by_file = agent_api.route(
        agent_request({"event": SAMPLE}),
        "POST", "/api/agent/designer/workflows/notify-video.yaml/test",
    )
    assert by_file["statusCode"] == 200
    assert json.loads(by_file["body"])["file"] == "notify-video.yaml"

    missing = agent_api.route(agent_request({}), "POST", "/api/agent/designer/workflows/test")
    assert missing["statusCode"] == 400
    not_object = agent_api.route(agent_request({"event": [1, 2]}), "POST",
                                 "/api/agent/designer/workflows/test")
    assert not_object["statusCode"] == 400


# ---- CLI ----

def test_workflows_test_dry_run(monkeypatch, tmp_path, capsys):
    from dapier_cli import commands

    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"file": "notify-video.yaml", "mode": "dry-run", "matched": True,
                "enabled": True, "ok": True,
                "steps": [{"action_id": "tell", "action_type": "slack", "ok": True,
                           "rendered_input": {"text": "New: Dry-run demo"}}]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    workflow_file = tmp_path / "wf.yaml"
    workflow_file.write_text("id: notify-video\n")
    code = commands.workflows_test("https://api.example.test", str(workflow_file),
                                   '{"title": "Dry-run demo"}')
    assert code == 0
    assert (seen["method"], seen["path"]) == ("POST", "/api/agent/designer/workflows/test")
    assert seen["body"] == {"yaml": "id: notify-video\n",
                            "event": {"title": "Dry-run demo"}, "execute": False}
    out, _ = capsys.readouterr()
    assert "Dry run: notify-video.yaml" in out
    assert "a trigger matches" in out
    assert "tell (slack): ok" in out
    assert '"text": "New: Dry-run demo"' in out


def test_workflows_test_event_file_and_execute(monkeypatch, tmp_path):
    from dapier_cli import commands

    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen["body"] = body
        return {"file": "wf.yaml", "mode": "execute", "matched": True, "ok": True,
                "steps": [{"action_id": "tell", "action_type": "slack", "ok": True,
                           "output": {"ts": "9"}}]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    workflow_file = tmp_path / "wf.yaml"
    workflow_file.write_text("id: wf\n")
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({"title": "from file"}))
    code = commands.workflows_test("https://api.example.test", str(workflow_file),
                                   f"@{event_file}", execute=True)
    assert code == 0
    assert seen["body"] == {"yaml": "id: wf\n", "event": {"title": "from file"}, "execute": True}


def test_workflows_test_reports_problems_with_exit_code(monkeypatch, tmp_path, capsys):
    from dapier_cli import commands

    monkeypatch.setattr(commands.api, "call", lambda *args, **kwargs: {
        "file": "wf.yaml", "mode": "dry-run", "matched": False, "ok": True,
        "steps": [{"action_id": "a1", "action_type": "slack", "ok": False,
                   "rendered_input": None, "error": "unsupported action: mystery"}],
    })
    workflow_file = tmp_path / "wf.yaml"
    workflow_file.write_text("id: wf\n")
    code = commands.workflows_test("https://api.example.test", str(workflow_file), "{}")
    assert code == 1
    out, _ = capsys.readouterr()
    assert "unsupported action: mystery" in out
    assert "NO trigger matches" in out


def test_workflows_test_input_errors(monkeypatch, tmp_path, capsys):
    from dapier_cli import commands

    assert commands.workflows_test("https://api", str(tmp_path / "nope.yaml"), "{}") == 2
    bad_event = tmp_path / "bad.json"
    bad_event.write_text("{not json")
    workflow_file = tmp_path / "wf.yaml"
    workflow_file.write_text("id: wf\n")
    assert commands.workflows_test("https://api", str(workflow_file), f"@{bad_event}") == 2
    assert commands.workflows_test("https://api", str(workflow_file), "[1, 2]") == 2
    out, _ = capsys.readouterr()
    assert "Invalid sample event JSON" in out
    assert "must be a JSON object" in out


def test_main_workflows_test_parsing(monkeypatch):
    from dapier_cli import commands, main

    seen = {}

    def fake_test(api_url, path, event_spec, execute=False, debug=False):
        seen.update(path=path, event=event_spec, execute=execute)
        return 0

    monkeypatch.setattr(commands, "workflows_test", fake_test)
    assert main.main(["workflows", "test", "wf.yaml", "--event", '{"a": 1}']) == 0
    assert seen == {"path": "wf.yaml", "event": '{"a": 1}', "execute": False}
    assert main.main(["workflows", "test", "wf.yaml", "--event", "@e.json", "--execute"]) == 0
    assert seen["execute"] is True and seen["event"] == "@e.json"
