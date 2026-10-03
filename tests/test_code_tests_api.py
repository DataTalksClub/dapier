"""Code-block tests: the admin, agent, and CLI surfaces that drive them
(the engine itself is covered in test_code_tests.py)."""

import json

import pytest

from src.dapier.api import admin
from src.dapier.api import agent as agent_api

from tests.test_test_run import (
    agent_request,
    admin_request,
    no_runners,
    operator_bearer,
    operator_session,
)

CODE_TESTS = [
    {"name": "empty", "input": {"attachments": []}, "expected": {"has_attachment": False}},
    {"name": "two fail", "input": {"attachments": [{}, {}]}, "expected_error": "got 2"},
]

CODE_WORKFLOW = {
    "id": "triage-demo",
    "enabled": True,
    "trigger": {"connector": "email", "event": "message.received"},
    "actions": [
        {"id": "triage", "type": "code",
         "code": ('attachments = input.get("attachments") or []\n'
                  'if len(attachments) > 1:\n'
                  '    raise ValueError(f"got {len(attachments)} attachments")\n'
                  'output = {"has_attachment": len(attachments) == 1}'),
         "tests": CODE_TESTS},
        {"id": "later", "type": "date_time", "value": "{date}"},
    ],
}


# ---- admin ----

def test_admin_test_code_runs_the_inline_draft(operator_session, no_runners):
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-code",
                      {"action_id": "triage", "workflow": CODE_WORKFLOW}),
        "POST", "/api/admin/designer/workflows/test-code",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["file"] == "triage-demo.yaml"
    assert payload["action_id"] == "triage"
    assert payload["total"] == 2
    assert payload["passed"] == 2
    assert payload["failed"] == 0
    assert [case["name"] for case in payload["cases"]] == ["empty", "two fail"]


@pytest.fixture
def code_bundle(tmp_path, monkeypatch):
    """The published store holding one workflow with a tested code step."""
    from src.dapier.api import designer_store
    from src.dapier.triggers import published_workflows

    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    monkeypatch.delenv(designer_store.TOKEN_SECRET_ENV, raising=False)
    item = {"workflow_id": "triage-demo", "file": "triage-demo.yaml",
            "workflow": CODE_WORKFLOW, "revision": 1}
    monkeypatch.setattr(published_workflows, "load_items", lambda table_ref=None: [item])
    monkeypatch.setattr(published_workflows, "get_item", lambda workflow_id, table_ref=None:
                        item if workflow_id == "triage-demo" else None)
    return tmp_path


def test_admin_test_code_saved_file(operator_session, code_bundle, no_runners):
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/triage-demo.yaml/test-code",
                      {"action_id": "triage"}),
        "POST", "/api/admin/designer/workflows/triage-demo.yaml/test-code",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["file"] == "triage-demo.yaml"
    assert payload["cases"][1]["ok"] is True


def test_admin_test_code_reports_failures(operator_session, no_runners):
    workflow = {**CODE_WORKFLOW, "actions": [
        {**CODE_WORKFLOW["actions"][0],
         "tests": [{"name": "wrong", "input": {}, "expected": {"nope": True}}]}]}
    response = admin.route(
        admin_request("POST", "/api/admin/designer/workflows/test-code",
                      {"action_id": "triage", "workflow": workflow}),
        "POST", "/api/admin/designer/workflows/test-code",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["failed"] == 1
    assert payload["cases"][0]["ok"] is False


def test_admin_test_code_rejects_bad_requests(operator_session, no_runners):
    def route(body):
        return admin.route(
            admin_request("POST", "/api/admin/designer/workflows/test-code", body),
            "POST", "/api/admin/designer/workflows/test-code",
        )

    assert route({})["statusCode"] == 400
    assert "action_id" in json.loads(route({})["body"])["error"]

    missing = route({"action_id": "later", "workflow": CODE_WORKFLOW})
    assert missing["statusCode"] == 400
    assert "code tests run on" in json.loads(missing["body"])["error"]

    no_tests = route({"action_id": "plain", "workflow": {**CODE_WORKFLOW, "actions": [
        {"id": "plain", "type": "code", "code": "1"}]}})
    assert no_tests["statusCode"] == 400
    assert "has no tests" in json.loads(no_tests["body"])["error"]

    unknown = route({"action_id": "nope", "workflow": CODE_WORKFLOW})
    assert unknown["statusCode"] == 400
    assert "no step named" in json.loads(unknown["body"])["error"]


# ---- agent (the CLI's route) ----

def test_agent_test_code_runs_inline(operator_bearer, no_runners):
    agent_api.reset_rate_limits()
    response = agent_api.route(
        agent_request({"action_id": "triage", "workflow": CODE_WORKFLOW}),
        "POST", "/api/agent/designer/workflows/test-code",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["passed"] == 2

    by_file = agent_api.route(
        agent_request({"action_id": "triage"}),
        "POST", "/api/agent/designer/workflows/triage-demo.yaml/test-code",
    )
    assert by_file["statusCode"] == 404  # nothing saved under that name here


def test_agent_test_code_requires_an_operator(monkeypatch):
    import boto3

    agent_api.reset_rate_limits()
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource",
                        lambda service: type("D", (), {"Table": lambda self, name: None})())
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {"sub": "u-1", "email": "u@example.test"})
    response = agent_api.route(
        agent_request({"action_id": "triage", "workflow": CODE_WORKFLOW}),
        "POST", "/api/agent/designer/workflows/test-code",
    )
    assert response["statusCode"] == 403


# ---- CLI ----

def test_workflows_test_code_reports_cases(monkeypatch, tmp_path, capsys):
    from dapier_cli import commands

    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"file": "triage-demo.yaml", "action_id": "triage",
                "total": 2, "passed": 1, "failed": 1,
                "cases": [
                    {"name": "empty", "ok": True},
                    {"name": "two fail", "ok": False, "error": "code step failed: "
                     "ValueError: got 2 attachments",
                     "expected": {"has_attachment": True}, "actual": None},
                ]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    workflow_file = tmp_path / "wf.yaml"
    workflow_file.write_text("id: triage-demo\n")
    code = commands.workflows_test_code("https://api.example.test", str(workflow_file),
                                        "triage")
    assert code == 1
    assert (seen["method"], seen["path"]) == ("POST", "/api/agent/designer/workflows/test-code")
    assert seen["body"] == {"yaml": "id: triage-demo\n", "action_id": "triage"}
    out, _ = capsys.readouterr()
    assert "Tested triage of triage-demo.yaml — 1/2 passed, 1 FAILED" in out
    assert "pass  empty" in out
    assert "FAIL  two fail" in out
    assert "got 2 attachments" in out

    monkeypatch.setattr(commands.api, "call", lambda *a, **k: {
        "file": "wf.yaml", "action_id": "triage", "total": 1, "passed": 1, "failed": 0,
        "cases": [{"name": "only", "ok": True}]})
    assert commands.workflows_test_code("https://api", str(workflow_file), "triage") == 0


def test_workflows_test_code_input_errors(tmp_path):
    from dapier_cli import commands

    assert commands.workflows_test_code("https://api", str(tmp_path / "nope.yaml"),
                                        "triage") == 2


def test_main_workflows_test_code_parsing(monkeypatch):
    from dapier_cli import commands, main

    seen = {}

    def fake_test_code(api_url, path, action_id, debug=False):
        seen.update(path=path, action=action_id, debug=debug)
        return 0

    monkeypatch.setattr(commands, "workflows_test_code", fake_test_code)
    assert main.main(["workflows", "test-code", "wf.yaml", "--action", "triage"]) == 0
    assert seen == {"path": "wf.yaml", "action": "triage", "debug": False}


# ---- the tests key survives the YAML round-trip the store performs ----

def test_yaml_round_trip_keeps_tests():
    import yaml

    from src.dapier.api.designer_store.validation import parse_workflow

    text = yaml.safe_dump(CODE_WORKFLOW, sort_keys=False)
    workflow = parse_workflow(text)
    assert workflow["actions"][0]["tests"] == CODE_TESTS
