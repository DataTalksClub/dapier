"""Telemetry redaction must never change credentials used by a running flow."""
import json

import boto3
import pytest
from moto import mock_aws

from dapier_cli import commands, main
from src.dapier.api import runs
from src.dapier.connectors import registry
from src.dapier.connectors.webhook import run_http_request
from src.dapier.engine import worker
from src.dapier.engine.actions.storage import kv_set, run_storage_get
from src.dapier.engine.history import redact
from src.dapier.engine.logic import CompletedStep, run_chain


@pytest.fixture
def tables(monkeypatch):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("STORAGE_TABLE", "storage")
    monkeypatch.delenv("TASK_USAGE_TABLE", raising=False)
    with mock_aws():
        db = boto3.resource("dynamodb")
        executions = db.create_table(
            TableName="executions", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"},
                                  {"AttributeName": "run_id", "AttributeType": "S"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "runs-by-run-id",
                "KeySchema": [{"AttributeName": "run_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"}}])
        db.create_table(
            TableName="storage", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "scope", "KeyType": "HASH"},
                       {"AttributeName": "key", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"}
                                  for name in ("scope", "key")])
        yield executions


@pytest.mark.parametrize("key,explicit", [
    ("webhook_token", False), ("client_SECRET", False), ("Password", False),
    ("AUTHORIZATION", False), ("service_Api_Key", False), ("ordinary", True),
])
def test_recorded_secret_is_masked_but_http_and_later_steps_get_real_value(
        tables, key, explicit, monkeypatch, capsys, tmp_path):
    token = "test-private-credential"
    kv_set("wf", key, token)
    event = {"id": "evt", "connector": "webhook", "event": "received", "data": {
        "auth_token": token, "headers": {"aUtHoRiZaTiOn": f"Bearer {token}"},
        "subject": "redaction verification"}}
    actions = [
        {"id": "get", "type": "storage_get", "key": key, "secret": explicit},
        {"id": "post", "type": "http_request", "url": "https://example.invalid",
         "method": "POST", "auth_type": "bearer",
         "auth_token": "{steps.get.output.value}"},
    ]
    received = []

    def transport(method, url, *, headers, body, timeout):
        received.append(headers["authorization"])
        return 204, b""

    def dispatch(action, event, workflow_id, steps=None):
        if action["type"] == "storage_get":
            return run_storage_get(action, event, workflow_id, steps=steps)
        assert steps["get"]["output"]["value"] == token
        return run_http_request(action, event, steps=steps, transport=transport)

    registry.validate_action_chain(actions)
    runtime = {}
    run_chain("wf", actions, event, dispatch, step_outputs=runtime,
              before_action=worker._is_pending, after_action=worker._mark_completed)
    assert received == [f"Bearer {token}"]
    assert runtime["get"]["output"]["value"] == token
    items = tables.scan()["Items"]
    assert token not in json.dumps(items, default=str)
    got = next(item for item in items if item["action_id"] == "get")
    assert got["output"]["value"] == f"<redacted len={len(token)}>"
    assert next(item for item in items if item["action_id"] == "post")["output"]["status"] == 204

    # Shared detail, list/search and export APIs drive both console and CLI.
    status, detail = runs.api_get("wf:evt")
    assert status == 200 and token not in json.dumps(detail, default=str)
    assert runs.api_list(q=token)[1]["runs"] == []
    assert runs.api_export(q=token)[1]["count"] == 0
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    def request(*args, **kwargs):
        return detail

    monkeypatch.setattr(commands.api, "call", request)
    assert main.main(["runs", "show", "wf:evt"]) == 0
    printed = capsys.readouterr().out
    assert token not in printed and "<redacted len=" in printed


def test_ordinary_storage_value_remains_visible(tables):
    output = {"key": "cursor", "value": "page-42", "found": True}
    worker._mark_completed("wf", "get", {"id": "evt"}, output=output)
    assert tables.scan()["Items"][0]["output"] == output


def test_old_records_are_redacted_in_details_and_search(tables):
    token = "older-private-credential"
    tables.put_item(Item={
        "execution_id": "wf:get:evt", "run_id": "wf:evt", "workflow_id": "wf",
        "action_id": "get", "action_type": "storage_get", "status": "completed",
        "started_at": "2026-10-24T12:00:00+00:00",
        "output": {"key": "webhook_token", "value": token, "found": True},
        "input": {"auth_token": token, "headers": {"Authorization": token}},
    })
    assert token not in json.dumps(runs.api_get("wf:evt"), default=str)
    assert runs.api_list(q=token)[1]["runs"] == []
    assert runs.api_export(q=token)[1]["count"] == 0


def test_redact_before_truncation_and_without_mutating_runtime():
    token = "s" * 9000
    data = {"headers": {"Authorization": token}, "auth_token": token}
    stored = worker._stored(data)
    assert stored == {"headers": {"Authorization": "<redacted len=9000>"},
                      "auth_token": "<redacted len=9000>"}
    assert data["auth_token"] == token
    assert redact(stored) == stored


def test_explicit_secret_does_not_leak_through_branch_summary_or_downstream_copy(tables):
    token = "test-private-credential"
    kv_set("wf", "ordinary", token)
    actions = [{"id": "branch", "type": "condition", "when": {"subject": {"equals": "go"}}, "then": [
        {"id": "get", "type": "storage_get", "key": "ordinary", "secret": True},
        {"id": "copy", "type": "copy"}]}]

    def dispatch(action, event, workflow_id, steps=None):
        if action["type"] == "storage_get":
            return run_storage_get(action, event, workflow_id, steps=steps)
        return {"copied": steps["branch.then.get"]["output"]["value"]}

    runtime = {}
    run_chain("wf", actions, {"id": "evt", "data": {"subject": "go"}}, dispatch, step_outputs=runtime,
              before_action=worker._is_pending, after_action=worker._mark_completed)
    assert runtime["branch.then.copy"]["output"]["copied"] == token
    assert token not in json.dumps(tables.scan()["Items"], default=str)


def test_deduped_storage_read_reloads_real_value(tables):
    kv_set("wf", "ordinary", "test-private-credential")
    runtime = {}
    run_chain("wf", [{"id": "get", "type": "storage_get", "key": "ordinary", "secret": True}],
              {"id": "evt"}, run_storage_get, step_outputs=runtime,
              before_action=lambda *args: CompletedStep("completed", {
                  "key": "ordinary", "value": "<redacted len=23>", "found": True}))
    assert runtime["get"]["output"]["value"] == "test-private-credential"


def test_replay_from_step_reloads_masked_storage_and_keeps_history_safe(tables, monkeypatch):
    token = "test-private-credential"
    kv_set("wf", "ordinary", token)
    monkeypatch.setattr(worker, "all_workflows", lambda: [{"id": "wf", "enabled": True}])
    monkeypatch.setattr(worker, "_quota_pending", worker._is_pending)
    monkeypatch.setattr(worker.failure_counts, "reset", lambda *args: None)
    masked = {"key": "ordinary", "value": "<redacted len=23>", "found": True}
    seen = []

    def dispatch(action, event, workflow_id, steps=None):
        if action["type"] == "storage_get":
            return run_storage_get(action, event, workflow_id, steps=steps)
        seen.append(steps["get"]["output"]["value"])
        return {"copied": seen[-1]}

    monkeypatch.setattr(worker, "_run_connector", dispatch)
    worker._resume_run({
        "workflow_id": "wf", "event": {"id": "evt", "connector": "webhook", "data": {}},
        "reused_steps": [{"action_id": "get", "action_type": "storage_get", "output": masked}],
        "step_outputs": {"get": {"status": "completed", "output": masked}},
        "segments": [{"steps": [{"id": "post", "type": "copy"}], "prefix": "", "scope": None}],
    })
    assert seen == [token]
    assert token not in json.dumps(tables.scan()["Items"], default=str)
