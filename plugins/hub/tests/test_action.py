import json
import sys
from types import ModuleType

import boto3
import pytest
from moto import mock_aws

import src.dapier.connectors  # noqa: F401
from plugins.hub import action
from src.dapier.connectors import registry


@pytest.fixture
def configured(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="hub-action-storage",
            KeySchema=[{"AttributeName": "scope", "KeyType": "HASH"},
                       {"AttributeName": "key", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": n, "AttributeType": "S"}
                                  for n in ("scope", "key")],
            BillingMode="PAY_PER_REQUEST")
        monkeypatch.setenv("STORAGE_TABLE", table.name)
        monkeypatch.setenv("HUB_INTAKE_TOKEN_SECRET_NAME", "dapier.hub")
        monkeypatch.setenv("HUB_INTAKE_URL", "https://hub.example.test/api/intake/tasks")
        secrets = ModuleType("src.dapier.secrets")
        secrets.value = lambda name: "fixture-bearer"
        monkeypatch.setitem(sys.modules, "src.dapier.secrets", secrets)
        yield table, secrets


def test_registered_action_dispatches_via_authenticated_intake_api(configured, monkeypatch):
    table, secrets = configured
    names, calls = [], []

    def value(name):
        names.append(name)
        return '{"token":"fixture-bearer"}'
    secrets.value = value

    def create(payload, *, token, url, transport):
        assert transport is action._transport
        calls.append((url, payload))
        assert token == "fixture-bearer"
        assert table.scan()["Count"] >= 1
        if url.endswith("/assign"):
            return 200, {"assigned_session": {
                "hub_session_id": "20000000-0000-4000-8000-000000000002", "state": "queued"}}
        return 201, {"task": {"id": "10000000-0000-4000-8000-000000000001", "version": 1}}
    monkeypatch.setattr(action.developer_webhook, "create_hub_intake", create)
    step = {"type": "hub_session", "id": "start", "title": "Handle {subject}",
            "text": "Task", "acceptance_criteria": "A deliverable"}
    registry.validate_action_chain([step])
    result = registry.run_action(step, {"id": "event", "data": {"subject": "hello"}}, "flow")
    assert result["state"] == "queued"
    assert names == ["dapier.hub"]
    assert calls[0][0] == "https://hub.example.test/api/intake/tasks"
    assert calls[0][1]["title"] == "Handle hello"
    assert calls[1][0].endswith("/10000000-0000-4000-8000-000000000001/assign")
    assert calls[1][1]["session"]["mode"] == "headless"


@pytest.mark.parametrize("raw", ["plain-token", '"json-token"',
                                 '{"token":"json-token"}', '{"access_token":"json-token"}'])
def test_team_store_bearer_formats(configured, raw):
    _, secrets = configured
    secrets.value = lambda name: raw
    assert action.service_token() in {"plain-token", "json-token"}


@pytest.mark.parametrize("raw", ["", " ", "contains whitespace", "{}", "null", "42", "[]"])
def test_malformed_secret_never_leaks_value(configured, raw):
    _, secrets = configured
    secrets.value = lambda name: raw
    with pytest.raises(ValueError, match="must contain a bearer token"):
        action.service_token()


def test_legacy_env_never_falls_back_to_secrets_manager(monkeypatch):
    monkeypatch.delenv("HUB_INTAKE_TOKEN_SECRET_NAME", raising=False)
    monkeypatch.setenv("HUB_INTAKE_TOKEN_SECRET_ID", "legacy-sm")
    monkeypatch.setenv("HUB_INTAKE_TOKEN", "fixture-env-token")
    with pytest.raises(ValueError, match="HUB_INTAKE_TOKEN_SECRET_NAME"):
        action.service_token()


@pytest.mark.parametrize("url", ["http://hub.example.test/tasks", "https://user:pass@hub.example.test/tasks",
                                 "https://hub.example.test/tasks?x=1", "https://hub.example.test/tasks#frag"])
def test_invalid_endpoint_fails_before_reading_token(monkeypatch, url):
    monkeypatch.setenv("HUB_INTAKE_URL", url)
    monkeypatch.setattr(action, "service_token", lambda: pytest.fail("credential read before URL validation"))
    with pytest.raises(ValueError, match="HTTPS endpoint"):
        action.run_hub_session({"id": "start"}, {}, "flow")


def test_required_criteria_and_engine_rejection_are_shared_save_rules():
    valid = {"type": "hub_session", "id": "start", "title": "Title", "text": "Task",
             "acceptance_criteria": "Deliver result"}
    for field in ("id", "title", "text", "acceptance_criteria"):
        with pytest.raises(registry.ActionError, match="missing"):
            registry.validate_action_chain([{**valid, field: " "}])
    with pytest.raises(registry.ActionError, match="unknown keys: engine"):
        registry.validate_action_chain([{**valid, "engine": "codex"}])
    with pytest.raises(registry.ActionError, match="not one of"):
        registry.validate_action_chain([{**valid, "tier": "custom"}])


def test_catalog_exposes_same_required_fields_to_all_clients():
    entry = next(e for e in registry.catalog()["actions"] if e["type"] == "hub_session")
    fields = {f["key"]: f for f in entry["fields"]}
    assert fields["acceptance_criteria"]["required"] is True
    assert fields["workspace"]["default"] == "dapier-agents"
    assert "engine" not in fields


def test_rendered_empty_criteria_fails_before_credential_lookup(configured, monkeypatch):
    monkeypatch.setattr(action, "service_token", lambda: pytest.fail("credential read for invalid task"))
    with pytest.raises(ValueError, match="non-empty acceptance_criteria"):
        action.run_hub_session({"id": "start", "title": "Title", "text": "Task",
                                "acceptance_criteria": "{missing}"}, {"id": "event"}, "flow")


def test_saved_result_needs_neither_credential_nor_hub(configured, monkeypatch):
    from plugins.hub.session import DynamoJournal, request_key

    table, _ = configured
    expected = {"hub_task_id": "saved-task", "hub_session_id": "saved-session", "state": "queued"}
    DynamoJournal(table, "flow").once(request_key("flow", "event", "start") + "/assigned", expected)
    monkeypatch.setattr(action, "service_token", lambda: pytest.fail("credential read for saved result"))
    assert action.run_hub_session({"id": "start"}, {"id": "event"}, "flow") == expected


def test_public_http_catalog_contains_hub_action():
    from src.dapier.api.router import handler

    response = handler({"requestContext": {"http": {"method": "GET", "path": "/api/catalog"}}}, None)
    assert response["statusCode"] == 200
    catalog = json.loads(response["body"])
    assert any(e["type"] == "hub_session" for e in catalog["actions"])


def test_cli_catalog_uses_shared_http_catalog(monkeypatch, capsys):
    from dapier_cli import commands, main

    calls = []

    def call(api_url, method, path, **kwargs):
        calls.append((method, path))
        return registry.catalog()
    monkeypatch.setattr(commands.api, "call", call)
    assert main.main(["catalog"]) == 0
    assert "hub_session" in capsys.readouterr().out
    assert calls == [("GET", "/api/catalog")]


def test_registered_runner_uses_qualified_loop_identity(configured, monkeypatch):
    from src.dapier.engine import logic_pkg as logic

    ids = []

    def launch(step, event, workflow_id, **kwargs):
        ids.append(step["id"])
        return {"hub_task_id": "10000000-0000-4000-8000-000000000001",
                "hub_session_id": "20000000-0000-4000-8000-000000000002"}
    monkeypatch.setattr(action, "launch", launch)
    logic.run_chain("flow", [{"id": "each", "type": "for_each", "list": "items", "actions": [
        {"id": "delegate", "type": "hub_session", "title": "Title", "text": "Task",
         "acceptance_criteria": "Deliver"}]}],
        {"id": "event", "data": {"items": ["first", "second"]}}, registry.run_action)
    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_generated_engine_identity_cannot_replace_required_explicit_id(configured, monkeypatch):
    from src.dapier.engine import logic_pkg as logic

    monkeypatch.setattr(action, "service_token", lambda: pytest.fail("credential read before explicit id"))
    with pytest.raises(ValueError, match="non-empty id"):
        logic.run_chain("flow", [{"type": "hub_session", "title": "Title", "text": "Task",
                                  "acceptance_criteria": "Deliver"}],
                        {"id": "event", "data": {}}, registry.run_action)


@pytest.mark.parametrize("changes", [{"id": ""}, {"acceptance_criteria": " "},
                                     {"acceptance_criteria": 1}, {"engine": "codex"}])
def test_actual_workflow_parse_rejects_invalid_hub_action(changes):
    import yaml
    from src.dapier.api.designer_store.validation import parse_workflow, WorkflowError

    step = {"type": "hub_session", "id": "start", "title": "Title", "text": "Task",
            "acceptance_criteria": "Deliver result", **changes}
    workflow = {"id": "hub-test", "trigger": {"connector": "webhook", "event": "hub-test"},
                "actions": [step]}
    with pytest.raises(WorkflowError, match="hub_session"):
        parse_workflow(yaml.safe_dump(workflow))


def test_actual_workflow_parse_accepts_templated_criteria():
    import yaml
    from src.dapier.api.designer_store.validation import parse_workflow

    workflow = {"id": "hub-test", "trigger": {"connector": "webhook", "event": "hub-test"},
                "actions": [{"type": "hub_session", "id": "start", "title": "Title", "text": "Task",
                             "acceptance_criteria": "{body.criteria}"}]}
    assert parse_workflow(yaml.safe_dump(workflow))["actions"][0]["acceptance_criteria"] == "{body.criteria}"


@pytest.mark.parametrize("redirect_status", [301, 302, 303, 307, 308])
def test_transport_refuses_redirect_without_sending_bearer_to_destination(redirect_status):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.send_response(redirect_status)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/credential-sink")
            self.end_headers()

        def do_GET(self):
            paths.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    try:
        thread.start()
        status, body = action._transport("POST", f"http://127.0.0.1:{server.server_port}/intake",
                                         headers={"authorization": "Bearer fixture-token"}, body=b"{}")
        assert status == redirect_status
        assert paths == ["/intake"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_designer_mirror_exposes_registered_fields():
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[3] / "designer/src/catalog.ts").read_text()
    match = re.search(r'type: "hub_session".*?fields:\s*\[(.*?)\n\s*\]', source, re.S)
    assert match
    keys = re.findall(r'key: "([^"]+)"', match.group(1))
    entry = next(e for e in registry.catalog()["actions"] if e["type"] == "hub_session")
    assert keys == [f["key"] for f in entry["fields"]]
    assert re.search(r'key: "acceptance_criteria"[^\n]*required: true', match.group(1))


def test_cli_can_save_hub_session_through_shared_workflow_api(monkeypatch, tmp_path):
    import yaml
    from dapier_cli import commands, main
    from src.dapier.api.designer_store.validation import parse_workflow

    workflow = {"id": "hub-test", "trigger": {"connector": "webhook", "event": "hub-test"},
                "actions": [{"type": "hub_session", "id": "start", "title": "Title", "text": "Task",
                             "acceptance_criteria": "Deliver result"}]}
    path = tmp_path / "hub-test.yaml"
    path.write_text(yaml.safe_dump(workflow))
    calls = []

    def call(api_url, method, endpoint, body, **kwargs):
        calls.append((method, endpoint))
        assert parse_workflow(body["yaml"])["actions"] == workflow["actions"]
        return {"file": "hub-test.yaml", "published": False}
    monkeypatch.setattr(commands.api, "call", call)
    assert main.main(["workflows", "save", str(path)]) == 0
    assert calls == [("PUT", "/api/agent/designer/workflows")]


def test_legacy_create_only_intake_survives_plugin_move(monkeypatch):
    from plugins.hub.intake_action import run_hub_intake
    from src.dapier.triggers.intake import developer_webhook

    monkeypatch.setattr(developer_webhook, "hub_bearer_token", lambda: "fixture-token")
    calls = []

    def transport(method, url, *, headers, body):
        calls.append(json.loads(body))
        return 201, {"task": {"id": "10000000-0000-4000-8000-000000000001", "version": 1}}
    result = run_hub_intake({"title": "File task", "text": "Instructions", "wake": False},
                            {"id": "event"}, transport=transport)
    assert result["hub_task_id"] == "10000000-0000-4000-8000-000000000001"
    assert result["assign"] is None
    assert len(calls) == 1
    assert registry.ACTIONS["hub_intake"].type == "hub_intake"
