"""scripts/drain_dlq.py lists every dead-lettered event and deletes only
the event ids an operator names; its grants stay scoped to the DLQ."""
import importlib.util
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _script():
    spec = importlib.util.spec_from_file_location("drain_dlq", ROOT / "scripts/drain_dlq.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSqs:
    def __init__(self, bodies):
        self.messages = [{"MessageId": f"m{i}", "ReceiptHandle": f"r{i}", "Body": body,
                          "Attributes": {"SentTimestamp": "1795000000000",
                                         "ApproximateReceiveCount": "5"}}
                         for i, body in enumerate(bodies)]
        self.deleted = []

    def receive_message(self, **kwargs):
        live = [m for m in self.messages if m["ReceiptHandle"] not in self.deleted]
        return {"Messages": live[:kwargs["MaxNumberOfMessages"]]}

    def delete_message(self, QueueUrl, ReceiptHandle):
        self.deleted.append(ReceiptHandle)


EMAIL = json.dumps({"Type": "Notification", "Message": json.dumps({
    "contract": "inbound-email", "event_id": "08674c39", "subject": "[CRITICAL] Hub canary"})})
WEBHOOK = json.dumps({"id": "5c044dd7", "connector": "webhook", "event": "request.received",
                      "data": {"subject": "Hello"}})


def test_lists_every_message_and_deletes_only_named_events(monkeypatch):
    module = _script()
    fake = FakeSqs([EMAIL, WEBHOOK, "not json"])
    listed, deleted = module.drain("url", {"08674c39"}, client=fake)
    assert [entry["event_id"] for entry in listed] == ["08674c39", "5c044dd7", None]
    assert listed[0]["connector"] == "email"
    assert listed[0]["subject"] == "[CRITICAL] Hub canary"
    assert listed[1]["event"] == "request.received"
    assert deleted == ["08674c39"]
    assert fake.deleted == ["r0"]


def test_list_only_deletes_nothing():
    module = _script()
    fake = FakeSqs([EMAIL, WEBHOOK])
    listed, deleted = module.drain("url", set(), client=fake)
    assert len(listed) == 2 and deleted == [] and fake.deleted == []


def test_drain_grants_cover_the_script_and_stay_on_the_dlq():
    script = (ROOT / "scripts/drain_dlq.py").read_text()
    sqs_calls = set(re.findall(r"\b(?:sqs|client)\.(\w+)\(", script))
    action = {"receive_message": "sqs:ReceiveMessage", "delete_message": "sqs:DeleteMessage",
              "get_queue_attributes": "sqs:GetQueueAttributes"}
    infra = yaml.safe_load((ROOT / "infra/dlq-drain-access.yaml").read_text().replace("!Sub", ""))
    policy = infra["Resources"]["GithubDeployDlqDrainAccess"]["Properties"]
    assert policy["Roles"] == ["dapier-github-actions-deploy"]
    (statement,) = policy["PolicyDocument"]["Statement"]
    assert sorted(statement["Action"]) == sorted(action[call] for call in sqs_calls)
    assert statement["Resource"].endswith(":dapier-DeadLetterQueue-*")

    deploy = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    runs = [step.get("run", "") for step in deploy["jobs"]["deploy"]["steps"]]
    assert any("infra/dlq-drain-access.yaml" in run for run in runs)
    dispatch = yaml.safe_load((ROOT / ".github/workflows/dlq-drain.yml").read_text())
    assert any("scripts/drain_dlq.py" in step.get("run", "")
               for step in dispatch["jobs"]["drain"]["steps"])
