import json
import unittest

import boto3
from botocore.exceptions import ClientError

from src.dapier.engine import worker
from src.dapier.engine.worker import normalize_payload


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "correlation_id": "corr-1",
}


class _CapturingTable:
    def __init__(self, calls, put_raises=None):
        self._calls = calls
        self._put_raises = put_raises

    def put_item(self, **kwargs):
        self._calls.append(("put_item", kwargs))
        if self._put_raises:
            raise self._put_raises

    def update_item(self, **kwargs):
        self._calls.append(("update_item", kwargs))


def _patch_table(monkeypatch, calls, put_raises=None):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return _CapturingTable(calls, put_raises)

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def test_is_pending_records_identity_and_start(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)

    assert worker._is_pending("youtube-slack", "notify", EVENT, "slack") is True
    item = calls[0][1]["Item"]
    assert item["execution_id"] == "youtube-slack:notify:evt-1"
    assert item["run_id"] == "youtube-slack:evt-1"
    assert item["workflow_id"] == "youtube-slack"
    assert item["action_id"] == "notify"
    assert item["action_type"] == "slack"
    assert item["connector"] == "email"
    assert item["event_type"] == "message.received"
    assert item["correlation_id"] == "corr-1"
    assert item["status"] == "processing"
    assert item["started_at"]
    assert item["input"] == {}


def test_is_pending_without_action_type_omits_field(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)

    worker._is_pending("youtube-slack", "notify", EVENT)
    assert "action_type" not in calls[0][1]["Item"]
    assert calls[0][1]["Item"]["run_id"] == "youtube-slack:evt-1"


def test_is_pending_false_when_lease_held(monkeypatch):
    calls = []
    conflict = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    _patch_table(monkeypatch, calls, put_raises=conflict)

    assert worker._is_pending("youtube-slack", "notify", EVENT) is False


def test_mark_completed_sets_finished(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)

    worker._mark_completed("youtube-slack", "notify", EVENT)
    kwargs = calls[0][1]
    assert calls[0][0] == "update_item"
    assert kwargs["ExpressionAttributeValues"][":completed"] == "completed"
    assert kwargs["ExpressionAttributeValues"][":finished"]
    assert ":output" not in kwargs["ExpressionAttributeValues"]


def test_mark_completed_records_output_and_duration(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)

    worker._mark_completed("youtube-slack", "notify", EVENT,
                           output={"ok": True, "ts": "1.2"}, duration_ms=123)
    kwargs = calls[0][1]
    values = kwargs["ExpressionAttributeValues"]
    assert values[":output"] == {"ok": True, "ts": "1.2"}
    assert values[":duration"] == 123
    assert kwargs["UpdateExpression"].startswith("SET ")


def test_release_action_records_error_and_reopens_lease(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)

    worker._release_action("youtube-slack", "notify", EVENT, RuntimeError("boom"))
    values = calls[0][1]["ExpressionAttributeValues"]
    assert calls[0][0] == "update_item"
    assert values[":failed"] == "failed"
    assert values[":error"] == "boom"
    assert values[":lease"] < int(__import__("time").time())
    assert ":duration" not in values


def test_release_action_records_failed_step_duration(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)

    worker._release_action("youtube-slack", "notify", EVENT, RuntimeError("boom"), duration_ms=45)
    assert calls[0][1]["ExpressionAttributeValues"][":duration"] == 45


def test_trim_keeps_small_values_and_previews_oversized_ones(monkeypatch=None):
    value = {"body": "x" * 50}
    assert worker._trim(value) == value

    big = {"body": "x" * 8000}
    trimmed = worker._trim(big, limit=100)
    assert trimmed["truncated"] is True
    assert len(trimmed["preview"]) == 100
    assert trimmed["preview"].startswith('{"body"')


class NormalizeTests(unittest.TestCase):
    def test_normalizes_raw_sns_inbound_email(self):
        email = {
            "contract": "inbound-email",
            "version": 1,
            "event_id": "event-1",
            "event_type": "email.received",
            "occurred_at": "2026-07-12T10:00:00Z",
            "route": "todo",
            "message_id": "message-1",
            "sender": {"addresses": ["sender@example.com"]},
            "recipients": {"matched": ["todo@example.com"]},
            "subject": "Do this",
            "date": "2026-07-12T09:59:00Z",
            "body": {"html": {"value": "<p>Do this</p>"}},
            "attachments": [],
            "raw_mime": {"bucket": "mail", "key": "raw/1"},
        }
        result = normalize_payload({"Type": "Notification", "Message": json.dumps(email)})
        self.assertEqual(result["schema_version"], "1.0")
        self.assertEqual(result["correlation_id"], "event-1")
        self.assertEqual(result["connector"], "email")
        self.assertEqual(result["data"]["route"], "todo")

    def test_normalizes_renderer_completion(self):
        result = normalize_payload({
            "schema": "html-renderer.completed.v1",
            "job_id": "job-1",
            "timestamp": "2026-07-12T10:00:00Z",
            "output": {"bucket": "renders", "key": "one.pdf"},
            "content_type": "application/pdf",
            "size_bytes": 42,
            "checksum": "abc",
            "context": {"source_event": {"data": {"message_id": "m1", "route": "invoice-pdf"}}},
        })
        self.assertEqual(result["event"], "job.completed")
        self.assertEqual(result["data"]["size_bytes"], 42)

    def test_rejects_unsupported_inbound_email_contract_version(self):
        with self.assertRaisesRegex(ValueError, "unsupported inbound-email contract version"):
            normalize_payload({"contract": "inbound-email", "version": 2})


if __name__ == "__main__":
    unittest.main()
