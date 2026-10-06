"""Routed jobs cannot be stolen by legacy or incompatible workers."""
import json

import boto3
import pytest
from moto import mock_aws

from dapier_cli import main, commands
from src.dapier import host_jobs, host_workers, headless_worker
from src.dapier.engine.actions.agent import run_agent, build_message
from src.dapier.worker_capabilities import capabilities


@pytest.mark.parametrize("value", [True, {}, ["Browser"], ["browser", None], "browser chrome"])
def test_invalid_capabilities(value):
    with pytest.raises(ValueError):
        capabilities(value)
    with pytest.raises(ValueError):
        build_message({"prompt": "p", "requires": value}, {"id": "e"}, "wf")


def test_workflow_save_validates_requirements():
    from src.dapier.api.designer_store.validation import _validate_steps, WorkflowError

    _validate_steps([{"type": "agent", "prompt": "p", "engine": "codex", "requires": ["browser"]}])
    with pytest.raises(WorkflowError, match="Capability names"):
        _validate_steps([{"type": "agent", "prompt": "p", "requires": "Browser"}])


def test_claim_rejects_malformed_capabilities():
    class Queue:
        pass
    assert host_jobs.claim("host", {"worker": {"worker_id": "w", "capabilities": True}},
                           table_ref=object(), queue_ref=Queue())[0] == 400


def test_routing_and_lease_lifecycle():
    with mock_aws():
        table = boto3.resource("dynamodb").create_table(
            TableName="host-tasks", KeySchema=[{"AttributeName": "task_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "task_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        queue = boto3.client("sqs")
        url = queue.create_queue(QueueName="host-jobs")["QueueUrl"]
        kwargs = {"table_ref": table, "queue_ref": queue, "queue_url": url}
        result = run_agent({"prompt": "Use Chrome", "engine": "codex",
                            "requires": ["browser", "chrome"]}, {"id": "e"}, "wf",
                           tasks=table, queue=queue, queue_url=url)
        assert queue.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]["ApproximateNumberOfMessages"] == "0"
        # A stale enqueue must not bypass the persisted requirements either.
        queue.send_message(QueueUrl=url, MessageBody=json.dumps({"kind": "agent", "task_id": result["task_id"]}))
        assert host_jobs.claim("old-host", now=999, **kwargs)[1]["job"] is None
        assert table.get_item(Key={"task_id": result["task_id"]})["Item"]["status"] == "queued"
        # Do not poll SQS in this test: any legacy claim still has no routed job.
        queue.receive_message = lambda **_: {}
        for meta in (None, {"worker_id": "old"},
                     {"worker_id": "server", "engine": "codex", "capabilities": []},
                     {"worker_id": "partial", "engine": "codex", "capabilities": ["browser"]},
                     {"worker_id": "wrong-engine", "engine": "claude", "capabilities": ["browser", "chrome"]}):
            assert host_jobs.claim("host", {"worker": meta}, now=1000, **kwargs)[1]["job"] is None
        meta = {"worker_id": "local", "engine": "codex", "capabilities": ["browser", "chrome"]}
        job = host_jobs.claim("host", {"worker": meta}, now=1000, **kwargs)[1]["job"]
        assert job["task_id"] == result["task_id"]
        assert host_jobs.claim("other", {"worker": meta}, now=1001, **kwargs)[1]["job"] is None
        assert host_jobs.heartbeat(job, "other", now=1010, **kwargs)[0] == 409
        assert host_jobs.heartbeat(job, "host", now=1010, **kwargs)[0] == 200
        assert host_jobs.finish({**job, "status": "succeeded"}, "host", now=1020, **kwargs)[0] == 200
        assert host_jobs.claim("host", {"worker": meta}, now=1021, **kwargs)[1]["job"] is None
        worker = host_workers.api_list(table_ref=table, now=1021)[1]["workers"][0]
        assert worker["capabilities"] == ["browser", "chrome"] and worker["engine"] == "codex"
        # Expired routed tasks are interrupted rather than re-executed.
        run_agent({"prompt": "p", "engine": "codex"}, {"id": "expired"}, "wf",
                  tasks=table, queue=queue, queue_url=url)
        expired = host_jobs.claim("host", {"worker": meta}, now=2000, **kwargs)[1]["job"]
        assert host_jobs.claim("host", {"worker": meta}, now=2200, **kwargs)[1]["job"] is None
        assert table.get_item(Key={"task_id": expired["task_id"]})["Item"]["status"] == "interrupted"


def test_cli_passes_worker_capabilities(monkeypatch):
    seen = []
    monkeypatch.setattr(commands, "worker_run", lambda *args, **kw: seen.append(kw) or 0)
    assert main.main(["worker", "--engine", "codex", "--capability", "browser", "--capability", "chrome", "--once"]) == 0
    assert seen[0]["engine"] == "codex" and seen[0]["capabilities"] == ["browser", "chrome"]


def test_worker_keeps_capabilities_in_presence(tmp_path):
    calls = []
    class Api:
        def call(self, op, body):
            calls.append(body)
            return {"job": None}
    headless_worker.serve(workspace_root=tmp_path, engine="codex", capabilities=["browser"], once=True, api=Api())
    assert calls[0]["worker"]["capabilities"] == ["browser"]
    assert calls[0]["worker"]["engine"] == "codex"
    assert "exec" in headless_worker.harness_argv("codex")


def test_codex_summary_uses_final_message(tmp_path):
    path = tmp_path / "out.jsonl"
    path.write_text('{"type":"item.completed","item":{"type":"agent_message","text":"Done"}}\n{"type":"turn.completed"}\n')
    assert headless_worker._summary(path, "succeeded", 0) == "Done"
