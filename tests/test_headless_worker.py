"""Host jobs complete under an HTTPS lease, without host AWS credentials."""

import base64
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
                                 "exit_code": 0, "logs": {"stdout": "x" * 13000, "stderr": "warning"}}, "token:host", table_ref=table,
                                queue_ref=queue, queue_url=url, now=1030)[0] == 200
        row = table.get_item(Key={"task_id": job["task_id"]})["Item"]
        assert row["status"] == "succeeded" and row["summary"] == "Draft saved"
        assert row["logs"] == {"stdout": "x" * 12000, "stderr": "warning", "truncated": True}
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
                             "notify_from": "agents@dtcdev.click",
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
        assert sent[0]["Source"] == "agents@dtcdev.click"
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
    assert calls[0]["Source"] == "no-reply@dtcdev.click"


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
    assert json.loads(result["logs"]["stdout"])["result"] == "Draft ready"
    assert result["logs"]["stderr"] == ""
    assert result["logs"]["truncated"] is False


def test_host_api_requires_the_dedicated_machine_token(monkeypatch):
    def authenticate(event):
        if event.get("machine"):
            event["_api_token"] = {"agent": event["machine"]}
        return "token:worker", None

    monkeypatch.setattr(agent_api, "authenticate", authenticate)
    monkeypatch.setattr(host_jobs, "claim", lambda owner, body=None: (200, {"job": None}))
    path = "/api/agent/host-jobs/claim"
    assert agent_api.route({}, "POST", path)["statusCode"] == 403
    assert agent_api.route({"machine": "some-agent"}, "POST", path)["statusCode"] == 403
    assert agent_api.route({"machine": "host-worker"}, "POST", path)["statusCode"] == 200


def test_attachment_route_shares_the_machine_token_gate(monkeypatch):
    def authenticate(event):
        if event.get("machine"):
            event["_api_token"] = {"agent": event["machine"]}
        return "token:worker", None

    monkeypatch.setattr(agent_api, "authenticate", authenticate)
    monkeypatch.setattr(host_jobs, "attachment",
                        lambda body, owner: (200, {"ok": True}))
    path = "/api/agent/host-jobs/attachment"
    assert agent_api.route({}, "POST", path)["statusCode"] == 403
    assert agent_api.route({"machine": "some-agent"}, "POST", path)["statusCode"] == 403
    assert agent_api.route({"machine": "host-worker"}, "POST", path)["statusCode"] == 200


def test_claim_serves_attachments_only_to_the_live_lease():
    with mock_aws():
        s3 = boto3.client("s3", region_name="eu-west-1")
        s3.create_bucket(Bucket="mail", CreateBucketConfiguration={
            "LocationConstraint": "eu-west-1"})
        s3.put_object(Bucket="mail", Key="raw/invoice.pdf", Body=b"%PDF-bytes!",
                      ContentType="application/pdf")
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="host-tasks",
            KeySchema=[{"AttributeName": "task_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "task_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        queue = boto3.client("sqs", region_name="eu-west-1")
        url = queue.create_queue(QueueName="host-jobs")["QueueUrl"]
        attachments = [{"filename": "invoice.pdf", "size": 11,
                        "content_type": "application/pdf",
                        "s3": {"bucket": "mail", "key": "raw/invoice.pdf"}}]
        message = {"kind": "agent", "task_id": "agent:flow:event:run",
                   "engine": "claude", "workspace": "", "prompt": "summarize",
                   "attachments": attachments}
        table.put_item(Item={**message, "status": "queued", "created_at": 1,
                             "notify_to": ""})
        queue.send_message(QueueUrl=url, MessageBody=json.dumps(message))
        job = host_jobs.claim("token:host", table_ref=table, queue_ref=queue,
                              queue_url=url, now=1000)[1]["job"]
        assert job["attachments"] == attachments

        def fetch(**overrides):
            body = {k: v for k, v in overrides.items() if v is not None}
            return host_jobs.attachment(
                {**job, "index": 0, "offset": 0, **body}, "token:host",
                table_ref=table, now=1010)[1]

        first = fetch(length=4)
        assert base64.b64decode(first["b64"]) == b"%PDF" and first["done"] is False
        middle = fetch(offset=4, length=4)
        assert middle["done"] is False
        last = fetch(offset=8, length=4)
        assert last["done"] is True
        # Chunks decode separately (padding lands mid-stream if concatenated).
        assert b"".join(base64.b64decode(c["b64"]) for c in (first, middle, last)) == b"%PDF-bytes!"
        assert last["size"] == 11 and last["filename"] == "invoice.pdf"
        # A stale, foreign, or finished lease gets nothing.
        assert host_jobs.attachment({**job, "index": 0}, "token:other",
                                    table_ref=table, now=1010)[0] == 409
        assert host_jobs.attachment({**job, "index": 0}, "token:host",
                                    table_ref=table, now=2000)[0] == 409
        assert host_jobs.attachment({**job, "index": 3}, "token:host",
                                    table_ref=table, now=1010)[0] == 404
        assert host_jobs.attachment({**job, "index": 0, "length": 5_000_001},
                                    "token:host", table_ref=table, now=1010)[0] == 400
        assert host_jobs.attachment({**job, "index": 0, "length": 0},
                                    "token:host", table_ref=table, now=1010)[0] == 400


def test_worker_stages_attachments_and_tells_the_agent(tmp_path):
    chunk_a = base64.b64encode(b"%PDF-inv").decode()
    chunk_b = base64.b64encode(b"oice body").decode()

    class Api:
        def call(self, operation, body=None):
            if operation != "attachment":
                return {}
            calls.append(body)
            if body["offset"] == 0:
                return {"b64": chunk_a, "done": False, "size": 15}
            return {"b64": chunk_b, "done": True, "size": 15}

    calls = []

    def fake_popen(_argv, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c",
             "import json,sys; print(json.dumps({'result': sys.stdin.read()}))"],
            **kwargs)

    job = {"task_id": "agent:flow:event:run", "lease_id": "lease-1",
           "engine": "claude", "workspace": "", "prompt": "summarize this",
           "attachments": [
               {"filename": "../escape/report.pdf", "s3": {"bucket": "m", "key": "r"}},
               {"filename": "report.pdf", "s3": {"bucket": "m", "key": "r2"}},
           ]}
    result = headless_worker.run_job(
        job, Api(), workspace_root=tmp_path, popen=fake_popen, sleep=lambda _: None)

    assert result["status"] == "succeeded"
    root = tmp_path / "attachments"
    assert sorted(p.name for p in root.iterdir()) == ["report-1.pdf", "report.pdf"]
    assert (root / "report.pdf").read_bytes() == b"%PDF-invoice body"
    assert (root / "report-1.pdf").read_bytes() == b"%PDF-invoice body"
    # Each attachment walks the chunks from offset 0.
    assert [c["offset"] for c in calls] == [0, 8, 0, 8]
    assert calls[0]["length"] == headless_worker.CHUNK_BYTES
    notice, _, prompt = result["summary"].partition("\n\n")
    assert notice.startswith("[Dapier] Trigger attachments saved to: ")
    assert "attachments/report.pdf" in notice and "attachments/report-1.pdf" in notice
    assert prompt == "summarize this"


def test_worker_fails_the_job_when_a_download_dries_up(tmp_path):
    class Api:
        def call(self, operation, body=None):
            return {"b64": "", "done": False}

    result = headless_worker.run_job(
        {"task_id": "agent:flow:event:run", "lease_id": "lease-1",
         "engine": "claude", "workspace": "", "prompt": "p",
         "attachments": [{"filename": "x.pdf", "s3": {"bucket": "m", "key": "k"}}]},
        Api(), workspace_root=tmp_path, popen=lambda *a, **k: None, sleep=lambda _: None)
    assert result["status"] == "failed"
    assert "empty chunk" in result["summary"]
    root = tmp_path / "root"
    root.mkdir()
    assert headless_worker.workspace_for(root, "") == root
    try:
        headless_worker.workspace_for(root, "../other")
    except ValueError as exc:
        assert "beneath" in str(exc)
    else:
        raise AssertionError("Workspace escaped the configured root")
