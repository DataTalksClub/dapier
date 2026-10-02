"""Persist successful file steps and restore their outputs across retries."""

import json
from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

from src.dapier.engine import logic, worker
from src.dapier.engine.actions.templating import render

CONFIG = Path(__file__).resolve().parents[1] / "workflows"


def fixture(name):
    return json.loads((CONFIG / "zapier" / "fixtures" / f"{name}.json").read_text())


def workflow(name):
    return yaml.safe_load((CONFIG / f"{name}.yaml").read_text())


@mock_aws
def test_completed_move_restores_output_after_expired_lease_without_repeating_side_effect(
    monkeypatch,
):
    table = boto3.resource("dynamodb").create_table(
        TableName="executions",
        KeySchema=[{"AttributeName": "execution_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "execution_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setattr(worker.usage, "enforce", lambda _wf: None)
    event = fixture("dropbox")
    calls = []
    actions = workflow("dropbox_on_upload")["actions"]

    def runner(action, event, workflow_id, steps=None):
        if action["type"] == "date_time":
            return {"formatted": "2026-10-02"}
        if action["type"] == "dropbox_move":
            src = render(action["from_path"], event, steps)
            dst = render(action["to_path"], event, steps)
            calls.append(src)
            return {"item": {"path": dst}, "moved": dst}
        if len(calls) == 2 and not getattr(runner, "recover", False):
            raise RuntimeError("intake temporarily failed")
        assert (
            render(action["path"], event, steps)
            == "/_dtc_paperwork/invoices/2026-10-02-deepseek.pdf"
        )
        return {"accepted": True}

    with pytest.raises(RuntimeError, match="intake temporarily failed"):
        logic.run_chain(
            "dropbox_on_upload", actions, event, runner, **worker._attempt_hooks(0)
        )
    for row in table.scan()["Items"]:
        table.update_item(
            Key={"execution_id": row["execution_id"]},
            UpdateExpression="SET lease_until = :old",
            ExpressionAttributeValues={":old": 0},
        )
    runner.recover = True
    logic.run_chain(
        "dropbox_on_upload", actions, event, runner, **worker._attempt_hooks(1)
    )
    assert calls == [
        "/_dtc_paperwork/invoices-landing/deepseek.pdf",
        "/_dtc_paperwork/invoices-landing/2026-10-02-deepseek.pdf",
    ]
    assert all(row["status"] == "completed" for row in table.scan()["Items"])


def test_completed_filter_recovery_still_stops_chain():
    calls = []
    result = logic.run_chain(
        "wf",
        [
            {"id": "gate", "type": "filter", "field": "x", "value": "yes"},
            {"id": "send", "type": "slack", "channel": "C"},
        ],
        {"data": {}},
        lambda *a, **kw: calls.append(a),
        before_action=lambda *a: logic.CompletedStep("filtered", {"filter": "stopped"}),
    )
    assert result == "filtered" and not calls
