"""GET /api/admin|agent/triggers/sample?workflow=<id>: the workflow's own
last trigger input, for the designer's {trigger.*} autofill and
`dapier triggers sample --workflow`.

The domain dispatch (runs.api_trigger_sample) prefers the newest run's
recorded input — what the workflow really received last — and falls back to
the trigger-discovery sample for the workflow's connector when nothing has
run yet. Both surfaces are covered: /api/agent/* with an operator-gated
bearer (API tokens never qualify) and /api/admin/* with a session cookie.
"""
import json
import time

import pytest

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import runs
from src.dapier.auth import session
from src.dapier.triggers import published_workflows

WORKFLOW_YAML = """\
id: sample-flow
enabled: true
trigger:
  connector: email
  event: message.received
actions:
  - id: a1
    type: webhook
    url: https://example.test/hook
"""


class FakeExecTable:
    """scan answers the ledger list; query answers the runs-by-run-id GSI."""

    def __init__(self, items):
        self.items = items

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def query(self, **kwargs):
        values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
        wanted = values[0] if values else None
        return {"Items": [item for item in self.items if item.get("run_id") == wanted]}


def _step(workflow_id, action_id, event_id, *, started="2026-09-25T10:00:00+00:00",
          connector="email", input_data=None, run_id=None):
    return {
        "execution_id": f"{workflow_id}:{action_id}:{event_id}",
        "run_id": run_id or f"{workflow_id}:{event_id}",
        "workflow_id": workflow_id,
        "action_id": action_id,
        "status": "completed",
        "started_at": started,
        "connector": connector,
        "event_type": "message.received",
        "input": input_data if input_data is not None else {"route": workflow_id},
    }


def _tables_env(monkeypatch, executions):
    tables = {"executions": FakeExecTable(executions)}

    class Dynamo:
        def Table(self, name):
            if name == "executions":
                return tables["executions"]
            return {"Items": []}

    import boto3

    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return tables


def write_workflow(tmp_path):
    monkeypatch_free = tmp_path / "sample-flow.yaml"
    monkeypatch_free.write_text(WORKFLOW_YAML)
    return tmp_path


# --- domain: newest run first, discovery only when nothing ran ---


def test_sample_returns_the_newest_run_recorded_input(monkeypatch):
    _tables_env(monkeypatch, [
        _step("sample-flow", "a1", "evt-1", started="2026-09-25T10:00:00+00:00",
              input_data={"subject": "old"}),
        _step("sample-flow", "a1", "evt-2", started="2026-09-26T11:00:00+00:00",
              input_data={"subject": "newest", "route": "todo"}),
        _step("other-flow", "a1", "evt-3", started="2026-09-27T09:00:00+00:00",
              input_data={"subject": "someone else's"}),
    ])
    status, payload = runs.api_trigger_sample("sample-flow")
    assert status == 200
    assert payload["source"] == "history"
    assert payload["workflow"] == "sample-flow"
    assert payload["run_id"] == "sample-flow:evt-2"
    assert payload["connector"] == "email"
    assert payload["event"] == "message.received"
    assert payload["data"] == {"subject": "newest", "route": "todo"}


def test_sample_falls_back_to_the_connector_discovery_sample(monkeypatch, tmp_path):
    _tables_env(monkeypatch, [])
    monkeypatch.setenv("WORKFLOWS_DIR", str(write_workflow(tmp_path)))
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    status, payload = runs.api_trigger_sample("sample-flow")
    assert status == 200
    assert payload["source"] == "synthetic"
    assert payload["workflow"] == "sample-flow"
    assert payload["connector"] == "email"
    assert payload["event"] == "message.received"
    assert isinstance(payload["data"], dict) and payload["data"]


def test_sample_is_404_when_no_runs_and_no_workflow(monkeypatch, tmp_path):
    _tables_env(monkeypatch, [])
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    status, payload = runs.api_trigger_sample("never-heard-of-it")
    assert status == 404
    assert "never-heard-of-it" in payload["error"]


def test_sample_requires_the_workflow_parameter(monkeypatch):
    _tables_env(monkeypatch, [])
    status, payload = runs.api_trigger_sample("")
    assert status == 400
    assert "workflow" in payload["error"]


# --- agent surface: /api/agent/triggers/sample (operator-gated bearer) ---


def configure_agent(monkeypatch, *, claims=None):
    agent_api.reset_rate_limits()
    tables = {"connections": {}, "grants": {}, "credentials": {},
              "api-tokens": {}, "executions": []}

    class Dynamo:
        def Table(self, name):
            if name == "executions":
                return FakeExecTable(tables["executions"])
            return _DictTable(tables.setdefault(name, {}))

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: dict(claims) if claims is not None else (_ for _ in ()).throw(ValueError("bad")),
    )
    return tables


class _DictTable:
    def __init__(self, items):
        self.items = items

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"].get("token_hash"))
        return {"Item": dict(item)} if item else {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item.get("token_hash") or item.get("connection_id")
                   or item.get("credential_id") or item.get("grantee")] = item

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}

    def query(self, **kwargs):
        value = kwargs.get("ExpressionAttributeValues", {}).get(":connection")
        return {"Items": [item for item in self.items.values()
                          if item.get("connection_id") == value]}


def agent_event(query=None, token="dtc-id-token"):
    headers = {"host": "dapier.example.test"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = {"headers": headers, "cookies": []}
    if query is not None:
        request["queryStringParameters"] = query
    return request


def test_agent_sample_requires_an_operator(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("OPERATOR_EMAILS", "boss@example.test")
    response = agent_api.route(
        agent_event(query={"workflow": "sample-flow"}), "GET", "/api/agent/triggers/sample")
    assert response["statusCode"] == 403


def test_agent_sample_rejects_api_token_callers(monkeypatch):
    """Operator gating is DTC-claims-only: a machine token never qualifies."""
    tables = configure_agent(monkeypatch, claims={"sub": "subject-1",
                                                  "email": "op@datatalks.club"})
    _, payload = agent_api.api_tokens.api_create(
        {"token_id": "ci", "agent": "buildcamp-uploader"}, "operator-1",
        table_ref=_DictTable(tables["api-tokens"]))
    response = agent_api.route(
        agent_event(query={"workflow": "sample-flow"}, token=payload["token"]),
        "GET", "/api/agent/triggers/sample")
    assert response["statusCode"] == 403


def test_agent_sample_returns_history_for_operators(monkeypatch):
    tables = configure_agent(monkeypatch, claims={"sub": "subject-1",
                                                  "email": "op@datatalks.club"})
    tables["executions"] = [
        _step("sample-flow", "a1", "evt-9", input_data={"subject": "hello there"}),
    ]
    response = agent_api.route(
        agent_event(query={"workflow": "sample-flow"}), "GET", "/api/agent/triggers/sample")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["source"] == "history"
    assert body["data"] == {"subject": "hello there"}


def test_agent_sample_404s_when_neither_source_applies(monkeypatch, tmp_path):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"})
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    response = agent_api.route(
        agent_event(query={"workflow": "ghost"}), "GET", "/api/agent/triggers/sample")
    assert response["statusCode"] == 404
    assert "ghost" in json.loads(response["body"])["error"]


def test_agent_sample_requires_the_workflow_parameter(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1", "email": "op@datatalks.club"})
    response = agent_api.route(agent_event(), "GET", "/api/agent/triggers/sample")
    assert response["statusCode"] == 400


# --- console surface: /api/admin/triggers/sample (session cookie) ---


def configure_admin(monkeypatch, executions=()):
    tables = {"executions": list(executions)}

    class Dynamo:
        def Table(self, name):
            if name == "executions":
                return FakeExecTable(tables["executions"])
            return {"Items": []}

    import boto3

    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(session, "_credentials",
                        lambda: {"username": "admin", "password": "pw"})
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    return [f"dapier_session={cookie}"]


def admin_request(method, path, query=None, cookies=None):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
    }
    if query is not None:
        event["queryStringParameters"] = query
    return event


def test_admin_sample_serves_the_same_payload(monkeypatch):
    cookies = configure_admin(monkeypatch, executions=[
        _step("sample-flow", "a1", "evt-2", input_data={"subject": "console too"}),
    ])
    response = admin.route(
        admin_request("GET", "/api/admin/triggers/sample",
                      query={"workflow": "sample-flow"}, cookies=cookies),
        "GET", "/api/admin/triggers/sample")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["source"] == "history"
    assert body["data"] == {"subject": "console too"}


def test_admin_sample_discovery_fallback_needs_no_runs(monkeypatch, tmp_path):
    cookies = configure_admin(monkeypatch)
    monkeypatch.setenv("WORKFLOWS_DIR", str(write_workflow(tmp_path)))
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    response = admin.route(
        admin_request("GET", "/api/admin/triggers/sample",
                      query={"workflow": "sample-flow"}, cookies=cookies),
        "GET", "/api/admin/triggers/sample")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["connector"] == "email"
    assert body["source"] in ("synthetic", "history")


# --- CLI: dapier triggers sample --workflow <id> (thin client) ---


def test_cli_workflow_sample(monkeypatch, tmp_path, capsys):
    from dapier_cli import commands, main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"workflow": "sample-flow", "source": "history",
                "connector": "email", "event": "message.received",
                "occurred_at": "2026-09-26T11:00:00+00:00",
                "data": {"subject": "Invoice", "route": "todo"}}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert main.main(["triggers", "sample", "--workflow", "sample-flow"]) == 0
    assert calls == [("GET", "/api/agent/triggers/sample?workflow=sample-flow")]
    out = capsys.readouterr().out
    assert "message.received" in out and "history" in out
    assert '"subject": "Invoice"' in out
    assert "{trigger.subject}" in out and "{trigger.route}" in out


def test_cli_workflow_sample_requires_connector_or_workflow(monkeypatch, tmp_path, capsys):
    from dapier_cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    assert main.main(["triggers", "sample"]) == 2
    assert "--workflow" in capsys.readouterr().out
