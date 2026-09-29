"""Host jobs complete under an HTTPS lease, without host AWS credentials."""

import json
import subprocess
import sys

import boto3
from moto import mock_aws

from src.dapier import headless_worker, host_jobs
from src.dapier.api import agent as agent_api


def test_claim_heartbeat_finish_and_no_second_execution():
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="host-tasks",
            KeySchema=[{"AttributeName": "task_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "task_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        queue = boto3.client("sqs", region_name="eu-west-1")
        url = queue.create_queue(QueueName="host-jobs")["QueueUrl"]
        message = {"kind": "agent", "task_id": "agent:flow:event:run",
                   "engine": "claude", "workspace": "", "prompt": "write a draft"}
        table.put_item(Item={**message, "status": "queued", "created_at": 1,
                             "notify_to": ""})
        queue.send_message(QueueUrl=url, MessageBody=json.dumps(message))
        status, payload = host_jobs.claim("token:host", table_ref=table,
                                           queue_ref=queue, queue_url=url, now=1000)
        assert status == 200
        job = payload["job"]
        assert job["prompt"] == "write a draft"
        assert "receipt" not in job
        assert table.get_item(Key={"task_id": job["task_id"]})["Item"]["status"] == "running"
        assert host_jobs.heartbeat({"task_id": job["task_id"], "lease_id": "wrong"},
                                   "token:host", table_ref=table, queue_ref=queue,
                                   queue_url=url, now=1020)[0] == 409
        assert host_jobs.heartbeat(job, "token:host", table_ref=table,
                                   queue_ref=queue, queue_url=url, now=1020)[0] == 200
        assert host_jobs.finish({**job, "status": "succeeded", "summary": "Draft saved",
                                 "exit_code": 0}, "token:wrong", table_ref=table,
                                queue_ref=queue, queue_url=url, now=1030)[0] == 409
        assert host_jobs.finish({**job, "status": "succeeded", "summary": "Draft saved",
                                 "exit_code": 0}, "token:host", table_ref=table,
                                queue_ref=queue, queue_url=url, now=1030)[0] == 200
        row = table.get_item(Key={"task_id": job["task_id"]})["Item"]
        assert row["status"] == "succeeded" and row["summary"] == "Draft saved"
        assert host_jobs.claim("token:host", table_ref=table, queue_ref=queue,
                               queue_url=url)[1]["job"] is None


def test_completion_emails_forwarding_sender(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="host-tasks",
            KeySchema=[{"AttributeName": "task_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "task_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        queue = boto3.client("sqs", region_name="eu-west-1")
        url = queue.create_queue(QueueName="host-jobs")["QueueUrl"]
        message = {"kind": "agent", "task_id": "agent:mail:event:run",
                   "prompt": "write a draft"}
        table.put_item(Item={**message, "status": "queued", "created_at": 1,
                             "notify_to": "writer@example.com",
                             "email_subject": "Zoom recording"})
        queue.send_message(QueueUrl=url, MessageBody=json.dumps(message))
        job = host_jobs.claim("token:host", table_ref=table, queue_ref=queue,
                              queue_url=url, now=1000)[1]["job"]
        sent = []

        class Ses:
            def send_email(self, **kwargs):
                sent.append(kwargs)

        monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dtcdev.click")
        assert host_jobs.finish({**job, "status": "succeeded", "summary": "Draft ready"},
                                "token:host", table_ref=table, queue_ref=queue,
                                queue_url=url, ses_ref=Ses(), now=1010)[0] == 200
        assert len(sent) == 1
        assert sent[0]["Destination"]["ToAddresses"] == ["writer@example.com"]
        assert "Draft ready" in sent[0]["Message"]["Body"]["Text"]["Data"]
        assert table.get_item(Key={"task_id": job["task_id"]})["Item"]["notified_at"]


def test_completion_email_uses_configured_ses_region(monkeypatch):
    calls = []

    class Ses:
        def send_email(self, **kwargs):
            calls.append(kwargs)

    def client(service, **kwargs):
        assert service == "ses"
        assert kwargs == {"region_name": "us-east-1"}
        return Ses()

    monkeypatch.setenv("DAPIER_EMAIL_REGION", "us-east-1")
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "no-reply@dtcdev.click")
    monkeypatch.setattr(boto3, "client", client)

    class Table:
        def update_item(self, **kwargs):
            pass

    host_jobs._notify(Table(), "task-1", {
        "status": "succeeded", "notify_to": "writer@example.com",
        "summary": "Done", "email_subject": "A task",
    })
    assert calls[0]["Destination"]["ToAddresses"] == ["writer@example.com"]


def test_worker_runs_foreground_harness_and_reports_result(tmp_path):
    calls = []

    class Api:
        def call(self, operation, body=None):
            calls.append((operation, body))
            return {}

    def fake_popen(_argv, **kwargs):
        assert "DAPIER_WORKER_TOKEN" not in kwargs["env"]
        return subprocess.Popen(
            [sys.executable, "-c", "import json,sys; sys.stdin.read(); "
             "print(json.dumps({'result':'Draft ready'}))"], **kwargs,
        )

    result = headless_worker.run_job(
        {"task_id": "agent:flow:event:run", "lease_id": "lease-1",
         "engine": "claude", "workspace": "", "prompt": "write a draft"},
        Api(), workspace_root=tmp_path, popen=fake_popen, sleep=lambda _: None,
    )
    assert result["status"] == "succeeded"
    assert result["summary"] == "Draft ready"
    assert calls[-1][0] == "finish"


def test_host_api_requires_the_dedicated_machine_token(monkeypatch):
    def authenticate(event):
        if event.get("machine"):
            event["_api_token"] = {"agent": event["machine"]}
        return "token:worker", None

    monkeypatch.setattr(agent_api, "authenticate", authenticate)
    monkeypatch.setattr(host_jobs, "claim", lambda owner: (200, {"job": None}))
    path = "/api/agent/host-jobs/claim"
    assert agent_api.route({}, "POST", path)["statusCode"] == 403
    assert agent_api.route({"machine": "some-agent"}, "POST", path)["statusCode"] == 403
    assert agent_api.route({"machine": "host-worker"}, "POST", path)["statusCode"] == 200


def test_explicit_workspace_cannot_escape_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert headless_worker.workspace_for(root, "") == root
    try:
        headless_worker.workspace_for(root, "../other")
    except ValueError as exc:
        assert "beneath" in str(exc)
    else:
        raise AssertionError("Workspace escaped the configured root")
