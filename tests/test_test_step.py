"""Per-step live testing (Zapier's "Test step"): the engine's test_step plus
the admin, agent, and CLI surfaces that drive it."""

import json

import pytest
import yaml

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import designer_store
from src.dapier.engine import dryrun

from tests.test_test_run import (  # reuse the shared fixtures/helpers
    SAMPLE,
    WORKFLOW,
    agent_request,
    no_runners,
    operator_bearer,
    operator_session,
    bundle,
    swap_runner,
)


# ---- engine ----

def test_step_dry_renders_without_touching_a_runner(no_runners):
    report = dryrun.test_step(WORKFLOW, "tell", SAMPLE)
    assert report["mode"] == "test-step"
    assert report["matched"] is True
    assert report["ok"] is True
    assert report["action_id"] == "tell"
    assert [step["action_id"] for step in report["steps"]] == ["tell"]
    step = report["steps"][0]
    assert step["rendered_input"]["text"] == "New: Dry-run demo at https://yt.test/x"
    assert "output" not in step


def test_step_execute_runs_only_that_step(monkeypatch):
    def fake_slack(action, event, steps=None):
        return {"ok": True, "ts": "42"}

    def never(action, event, steps=None):
        raise AssertionError("a step test ran a different step")

    swap_runner(monkeypatch, "slack", fake_slack)
    swap_runner(monkeypatch, "dataops", never)
    report = dryrun.test_step(WORKFLOW, "tell", SAMPLE, execute=True)
    assert report["mode"] == "execute-step"
    assert report["ok"] is True
    assert report["steps"][0]["output"] == {"ok": True, "ts": "42"}
    assert "duration_ms" in report["steps"][0]


def test_step_execute_reports_a_failing_step(monkeypatch):
    def failing(action, event, steps=None):
        raise RuntimeError("no connection configured")

    swap_runner(monkeypatch, "slack", failing)
    report = dryrun.test_step(WORKFLOW, "tell", SAMPLE, execute=True)
    assert report["ok"] is False
    assert report["steps"][0]["error"] == "no connection configured"


def test_step_feeds_prior_outputs_to_templates_and_runner(monkeypatch):
    from dataclasses import replace

    from src.dapier.connectors import registry

    seen = {}

    def fake_slack(action, event, workflow_id=None, steps=None):
        seen["steps"] = steps
        return {"ok": True}

    monkeypatch.setitem(registry.ACTIONS, "slack",
                        replace(registry.ACTIONS["slack"], run=fake_slack))
    outputs = {"prep": {"status": "completed", "output": {"key": "a/b.txt"}}}
    report = dryrun.test_step(
        WORKFLOW, "tell", SAMPLE, execute=True, step_outputs=outputs)
    assert report["ok"] is True
    assert seen["steps"] == outputs
    assert report["steps"][0]["rendered_input"]["text"] == "New: Dry-run demo at https://yt.test/x"

    workflow = {**WORKFLOW, "actions": [
        {"id": "tell", "type": "slack", "channel": "#videos",
         "text": "put {steps.prep.output.key}"},
    ]}
    report = dryrun.test_step(workflow, "tell", SAMPLE, step_outputs=outputs)
    assert report["steps"][0]["rendered_input"]["text"] == "put a/b.txt"


def test_step_unknown_id_is_an_error(no_runners):
    with pytest.raises(dryrun.TestRunError, match="no step named 'nope'"):
        dryrun.test_step(WORKFLOW, "nope", SAMPLE)
    with pytest.raises(dryrun.TestRunError, match="action id"):
        dryrun.test_step(WORKFLOW, "  ", SAMPLE)


def test_step_evaluates_logic_steps_without_running_them(no_runners):
    workflow = {"id": "logic-wf",
                "trigger": {"connector": "email", "event": "message.received"},
                "actions": [
        {"id": "route", "type": "paths", "paths": [
            {"label": "invoices", "when": {"subject": {"contains": "invoice"}},
             "actions": [{"id": "inner", "type": "slack", "channel": "#c", "text": "x"}]},
        ]},
        {"id": "each", "type": "for_each", "list": "files",
         "actions": [{"id": "body", "type": "slack", "channel": "#c", "text": "y"}]},
    ]}
    report = dryrun.test_step(workflow, "route", {"subject": "invoice 7"})
    assert report["ok"] is True
    assert report["steps"][0]["output"] == {
        "matched": "invoices", "steps": 1,
        "note": "branch not executed — test its steps individually",
    }

    report = dryrun.test_step(workflow, "route.invoices.inner",
                              {"subject": "invoice 7"})
    assert report["ok"] is True
    assert report["steps"][0]["action_type"] == "slack"

    report = dryrun.test_step(workflow, "each", {"files": ["a", "b"]})
    assert report["steps"][0]["output"]["items"] == 2
    assert report["steps"][0]["output"]["iterated"] == 0

    report = dryrun.test_step(workflow, "route", {"subject": "receipt"})
    assert report["steps"][0]["output"]["matched"] is None


def test_step_filter_reports_pass_and_filtered(no_runners):
    workflow = {**WORKFLOW, "actions": [
        {"id": "keep", "type": "filter", "when": {"title": {"prefix": "Dry"}}},
    ]}
    passed = dryrun.test_step(workflow, "keep", SAMPLE)
    assert passed["ok"] is True
    assert passed["steps"][0]["status"] == "completed"
    stopped = dryrun.test_step(workflow, "keep", {"title": "Receipt"})
    assert stopped["ok"] is False
    assert stopped["steps"][0]["status"] == "filtered"
    assert "filter stops the chain" in stopped["steps"][0]["error"]


def test_step_delay_reports_the_wait_without_sleeping(no_runners, monkeypatch):
    def no_sleep(seconds):
        raise AssertionError(f"a step test slept {seconds}s")

    monkeypatch.setattr("time.sleep", no_sleep)
    workflow = {**WORKFLOW, "actions": [{"id": "wait", "type": "delay", "minutes": 30}]}
    report = dryrun.test_step(workflow, "wait", SAMPLE)
    assert report["ok"] is True
    output = report["steps"][0]["output"]
    assert output["delay_seconds"] == 1800
    assert output["suspended"] is True
    assert output["resume_at"]

    workflow = {**WORKFLOW, "actions": [{"id": "wait", "type": "delay", "seconds": 5}]}
    report = dryrun.test_step(workflow, "wait", SAMPLE)
    assert report["steps"][0]["output"] == {
        "delay_seconds": 5, "slept_seconds": 0,
        "note": "not slept — a step test never waits",
    }


def test_step_addresses_error_branches(no_runners, monkeypatch):
    ran = []

    def fake_slack(action, event, steps=None):
        ran.append(action["id"])
        return {"ok": True}

    swap_runner(monkeypatch, "slack", fake_slack)
    workflow = {**WORKFLOW, "actions": [
        {"id": "send", "type": "slack", "channel": "#c", "text": "{title}",
         "on_error": "run",
         "error_actions": [{"id": "alert", "type": "slack", "channel": "#alerts",
                            "text": "failed: {steps.send.error}"}]},
    ]}
    report = dryrun.test_step(workflow, "send.error.alert", SAMPLE, execute=True)
    assert report["ok"] is True
    assert ran == ["alert"]
    assert report["steps"][0]["rendered_input"]["text"] == "failed: "


def test_step_reports_unsupported_types_and_code_bodies_verbatim(no_runners):
    workflow = {**WORKFLOW, "actions": [
        {"id": "mystery", "type": "mystery_action", "x": "{title}"},
    ]}
    report = dryrun.test_step(workflow, "mystery", SAMPLE)
    assert report["ok"] is False
    assert report["steps"][0]["error"] == "unsupported action: mystery_action"
    assert report["steps"][0]["rendered_input"]["x"] == "Dry-run demo"

    code = "return { title: data.title };"
    workflow = {**WORKFLOW, "actions": [{"id": "js", "type": "js", "code": code}]}
    report = dryrun.test_step(workflow, "js", SAMPLE)
    assert report["steps"][0]["rendered_input"]["code"] == code


# ---- admin API ----

def admin_request(method, path, body=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "body": json.dumps(body) if body is not None else None,
    }


def test_admin_test_step_executes_the_inline_draft(operator_session, monkeypatch):
    def fake_slack(action, event, steps=None):
        return {"ok": True, "ts": "7"}

    swap_runner(monkeypatch, "slack", fake_slack)
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-step",
                      {"action_id": "tell", "event": SAMPLE, "execute": True,
                       "workflow": WORKFLOW}),
        "POST", "/api/admin/designer/workflows/test-step",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["file"] == "notify-video.yaml"
    assert payload["mode"] == "execute-step"
    assert payload["action_id"] == "tell"
    assert payload["ok"] is True
    assert payload["steps"][0]["output"] == {"ok": True, "ts": "7"}


def test_admin_test_step_file_fallback_and_validation(operator_session, bundle, no_runners):
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/notify-video.yaml/test-step",
                      {"action_id": "prep", "event": SAMPLE}),
        "POST", "/api/admin/designer/workflows/notify-video.yaml/test-step",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["file"] == "notify-video.yaml"
    assert payload["steps"][0]["rendered_input"]["title"] == "Dry-run demo"

    no_action = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-step", {"event": SAMPLE}),
        "POST", "/api/admin/designer/workflows/test-step",
    )
    assert no_action["statusCode"] == 400
    assert "action_id" in json.loads(no_action["body"])["error"]

    no_event = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-step", {"action_id": "prep"}),
        "POST", "/api/admin/designer/workflows/test-step",
    )
    assert no_event["statusCode"] == 400

    bad_steps = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-step",
                      {"action_id": "prep", "event": SAMPLE, "steps": {"prep": "nope"}}),
        "POST", "/api/admin/designer/workflows/test-step",
    )
    assert bad_steps["statusCode"] == 400
    assert "steps" in json.loads(bad_steps["body"])["error"]

    unknown = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/notify-video.yaml/test-step",
                      {"action_id": "ghost", "event": SAMPLE}),
        "POST", "/api/admin/designer/workflows/notify-video.yaml/test-step",
    )
    assert unknown["statusCode"] == 400
    assert "no step named 'ghost'" in json.loads(unknown["body"])["error"]


def test_admin_test_step_requires_a_session(monkeypatch):
    from src.dapier.auth import session

    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-step",
                      {"action_id": "tell", "event": SAMPLE}),
        "POST", "/api/admin/designer/workflows/test-step",
    )
    assert response["statusCode"] == 401


# ---- agent API ----

def test_agent_test_step_requires_an_operator_bearer(monkeypatch):
    import boto3

    agent_api.reset_rate_limits()
    monkeypatch.setattr(boto3, "resource", lambda service: type("D", (), {"Table": lambda self, name: None})())
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "someone", "email": "agent@example.test"},
    )
    response = agent_api.route(
        agent_request({"action_id": "tell", "event": SAMPLE, "workflow": WORKFLOW}),
        "POST", "/api/agent/designer/workflows/test-step",
    )
    assert response["statusCode"] == 403


def test_agent_test_step_execute_over_bearer(operator_bearer, monkeypatch, bundle):
    def fake_slack(action, event, steps=None):
        return {"ok": True, "ts": "9"}

    swap_runner(monkeypatch, "slack", fake_slack)
    body = {"action_id": "tell", "event": SAMPLE, "workflow": WORKFLOW, "execute": True}
    response = agent_api.route(agent_request(body), "POST",
                               "/api/agent/designer/workflows/test-step")
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["mode"] == "execute-step"
    assert payload["steps"][0]["output"] == {"ok": True, "ts": "9"}

    by_file = agent_api.route(
        agent_request({"action_id": "prep", "event": SAMPLE}),
        "POST", "/api/agent/designer/workflows/notify-video.yaml/test-step",
    )
    assert by_file["statusCode"] == 200
    assert json.loads(by_file["body"])["steps"][0]["action_type"] == "dataops"


# ---- CLI ----

def test_workflows_test_step_dry_and_execute(monkeypatch, tmp_path, capsys):
    from dapier_cli import commands

    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"file": "notify-video.yaml", "mode": "execute-step", "ok": True,
                "action_id": "tell",
                "steps": [{"action_id": "tell", "action_type": "slack", "ok": True,
                           "rendered_input": {"text": "New: Dry-run demo"},
                           "output": {"ok": True, "ts": "9"}}]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    workflow_file = tmp_path / "wf.yaml"
    workflow_file.write_text("id: notify-video\n")
    steps_file = tmp_path / "steps.json"
    steps_file.write_text(json.dumps({"prep": {"status": "completed", "output": {}}}))

    code = commands.workflows_test_step(
        "https://api.example.test", str(workflow_file), "tell", '{"title": "x"}',
        steps_spec=f"@{steps_file}", execute=True)
    assert code == 0
    assert (seen["method"], seen["path"]) == ("POST", "/api/agent/designer/workflows/test-step")
    assert seen["body"] == {"yaml": "id: notify-video\n", "action_id": "tell",
                            "event": {"title": "x"}, "execute": True,
                            "steps": {"prep": {"status": "completed", "output": {}}}}
    out, _ = capsys.readouterr()
    assert "Executed step tell of notify-video.yaml — ok" in out
    assert '"ts": "9"' in out

    monkeypatch.setattr(commands.api, "call", lambda *a, **k: {
        "file": "wf.yaml", "ok": False, "action_id": "tell",
        "steps": [{"action_id": "tell", "action_type": "slack", "ok": False,
                   "error": "no connection configured"}]})
    assert commands.workflows_test_step("https://api", str(workflow_file), "tell", "{}") == 1
    out, _ = capsys.readouterr()
    assert "Tested step tell" in out
    assert "no connection configured" in out


def test_workflows_test_step_input_errors(monkeypatch, tmp_path, capsys):
    from dapier_cli import commands

    assert commands.workflows_test_step("https://api", str(tmp_path / "nope.yaml"),
                                        "tell", "{}") == 2
    workflow_file = tmp_path / "wf.yaml"
    workflow_file.write_text("id: wf\n")
    assert commands.workflows_test_step("https://api", str(workflow_file),
                                        "tell", "[1, 2]") == 2
    assert commands.workflows_test_step("https://api", str(workflow_file),
                                        "tell", "{not json") == 2
    steps_file = tmp_path / "bad.json"
    steps_file.write_text("{not json")
    assert commands.workflows_test_step("https://api", str(workflow_file), "tell",
                                        "{}", steps_spec=f"@{steps_file}") == 2
    out, _ = capsys.readouterr()
    assert "Invalid sample event JSON" in out
    assert "The sample event must be a JSON object." in out
    assert "Invalid steps JSON" in out


def test_main_workflows_test_step_parsing(monkeypatch):
    from dapier_cli import commands, main

    seen = {}

    def fake_test_step(api_url, path, action_id, event_spec, steps_spec=None,
                       execute=False, debug=False):
        seen.update(path=path, action=action_id, event=event_spec,
                    steps=steps_spec, execute=execute)
        return 0

    monkeypatch.setattr(commands, "workflows_test_step", fake_test_step)
    assert main.main(["workflows", "test-step", "wf.yaml", "--action", "tell",
                      "--event", '{"a": 1}']) == 0
    assert seen == {"path": "wf.yaml", "action": "tell", "event": '{"a": 1}',
                    "steps": None, "execute": False}
    assert main.main(["workflows", "test-step", "wf.yaml", "--action", "tell",
                      "--event", "@e.json", "--steps", "@s.json", "--execute"]) == 0
    assert seen["execute"] is True and seen["steps"] == "@s.json"
