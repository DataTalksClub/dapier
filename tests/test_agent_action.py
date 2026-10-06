"""The agent action enqueues a host job and does not import Aplexer."""
import json
import sys

from src.dapier.engine.actions.agent import build_message, run_agent


def test_importing_the_action_does_not_import_aplexer():
    import src.dapier.connectors.agent  # noqa: F401
    import src.dapier.engine.actions.agent  # noqa: F401
    assert "aplexer" not in sys.modules


def test_email_and_webhook_events_enqueue_a_queued_agent_job(monkeypatch, tmp_path):
    sent = []

    class Queue:
        def send_message(self, **kwargs):
            sent.append(kwargs)

    class Tasks:
        def __init__(self):
            self.items = {}

        def put_item(self, Item, ConditionExpression=None):
            self.items[Item["task_id"]] = dict(Item)

    tasks = Tasks()
    action = {
        "id": "wake",
        "type": "agent",
        "prompt": "{subject}\n\n{text}",
        "workspace": str(tmp_path),
        "engine": "claude",
    }
    email = {
        "id": "email:1", "connector": "email", "event": "message.received",
        "data": {"subject": "Invoice", "text": "please"},
    }
    hook = {
        "id": "hook:9", "connector": "webhook", "event": "received",
        "data": {"subject": "Ping", "text": "go"},
    }
    for event, workflow in ((email, "email-trigger-agent"), (hook, "orders")):
        sent.clear()
        result = run_agent(action, event, workflow, queue=Queue(), tasks=tasks,
                           queue_url="https://queue.test/host")
        body = json.loads(sent[0]["MessageBody"])
        assert body["kind"] == "agent"
        assert body["task_id"] == f"agent:{workflow}:{event['id']}:wake"
        assert body["engine"] == "claude"
        assert body["workspace"] == str(tmp_path)
        assert body["tag_prefix"] == "agent"
        assert "Invoice" in body["prompt"] or "Ping" in body["prompt"]
        assert result == {"task_id": body["task_id"], "status": "queued"}
        assert tasks.items[body["task_id"]]["status"] == "queued"
    built = build_message(action, email, "email-trigger-agent")
    assert built["prompt"].startswith("Invoice")


def test_template_host_queue_is_send_only():
    text = open("template.yaml", encoding="utf-8").read()
    assert "HostQueue:" in text
    assert "VisibilityTimeout: 120" in text
    assert "maxReceiveCount: 5" in text
    assert "HostDeadLetterQueue:" in text
    assert "HostDeadLetterAlarm:" in text
    # The API leases on behalf of host workers and enqueues agent tasks from
    # API-side paths (test-execute, redo rows), so its lease statement also
    # sends.
    assert (
        "Action: [sqs:ReceiveMessage, sqs:DeleteMessage, sqs:ChangeMessageVisibility,"
        in text
    )
    assert "sqs:SendMessage]" in text
    # The workflow function may send, and does not subscribe.
    assert "QueueName: !GetAtt HostQueue.QueueName" in text
    worker = text.split("WorkerFunction:", 1)[1].split("\n  # Standalone", 1)[0]
    assert "Queue: !GetAtt HostQueue.Arn" not in worker
    assert "sqs:ReceiveMessage" not in worker


def test_email_action_can_use_the_host_root_and_reply_to_sender():
    message = build_message(
        {"type": "agent", "prompt": "{body.text.value}",
         "notify_from": "agents@dtcdev.click"},
        {"id": "e1", "connector": "email", "data": {
            "body": {"text": {"value": "Draft from this Zoom recording"}},
            "sender": {"addresses": ["writer@example.com"]},
            "subject": "Zoom assets", "message_id": "<mail-1@example.com>",
        }},
        "email-trigger-agents",
    )
    assert message["workspace"] == ""
    assert message["prompt"] == "Draft from this Zoom recording"
    assert message["notify_to"] == "writer@example.com"
    assert message["notify_from"] == "agents@dtcdev.click"
    assert message["email_subject"] == "Zoom assets"


def test_agent_rejects_display_name_in_completion_sender():
    import pytest

    with pytest.raises(ValueError, match="notify_from must be one email address"):
        build_message(
            {"type": "agent", "prompt": "hello",
             "notify_from": "Agent <agents@dtcdev.click>"},
            {"id": "e1", "connector": "email", "data": {}},
            "email-trigger-agents",
        )


STORED = {"filename": "../../etc/in voice.pdf", "size": 12,
          "content_type": "application/pdf", "s3": {"bucket": "mail", "key": "a/b"}}


def test_trigger_attachments_ride_the_host_job():
    sent = []

    class Queue:
        def send_message(self, **kwargs):
            sent.append(kwargs)

    class Tasks:
        def put_item(self, Item, ConditionExpression=None):
            Tasks.item = dict(Item)

    event = {"id": "email:1", "connector": "email", "data": {
        "subject": "Invoice", "attachments": [STORED, "junk", {"filename": "stub.pdf"}]}}
    result = run_agent({"id": "wake", "type": "agent", "prompt": "{subject}"},
                       event, "wf", queue=Queue(), tasks=Tasks(), queue_url="q")
    body = json.loads(sent[0]["MessageBody"])
    assert body["attachments"] == Tasks.item["attachments"] == [{
        "filename": "in voice.pdf", "size": 12,
        "content_type": "application/pdf", "s3": {"bucket": "mail", "key": "a/b"},
    }]
    assert result == {"task_id": body["task_id"], "status": "queued"}


def test_attachments_are_skipped_when_off_and_capped_at_ten():
    off = build_message({"type": "agent", "prompt": "p", "attachments": "off"},
                        {"id": "e", "data": {"attachments": [STORED]}}, "wf")
    assert off["attachments"] == []
    many = build_message(
        {"type": "agent", "prompt": "p"},
        {"id": "e", "data": {"attachments": [
            {"filename": f"f{i}.pdf", "s3": {"bucket": "mail", "key": str(i)}}
            for i in range(15)]}}, "wf")
    assert [a["filename"] for a in many["attachments"]] == [
        f"f{i}.pdf" for i in range(10)]
    none = build_message({"type": "agent", "prompt": "p"},
                         {"id": "h", "connector": "webhook", "data": {}}, "wf")
    assert none["attachments"] == []
