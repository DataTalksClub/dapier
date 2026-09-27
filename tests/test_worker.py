import json
import time
import unittest

import boto3
import pytest
from botocore.exceptions import ClientError

from src.dapier.engine import notify
from src.dapier.engine import worker
from src.dapier.engine.worker import normalize_payload


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "correlation_id": "corr-1",
}


class _CapturingTable:
    def __init__(self, calls, put_raises=None, existing=None):
        self._calls = calls
        self._put_raises = put_raises
        self._existing = existing or {}

    def put_item(self, **kwargs):
        self._calls.append(("put_item", kwargs))
        if self._put_raises:
            raise self._put_raises

    def update_item(self, **kwargs):
        self._calls.append(("update_item", kwargs))

    def get_item(self, **kwargs):
        self._calls.append(("get_item", kwargs))
        return {"Item": dict(self._existing)} if self._existing else {}


def _patch_table(monkeypatch, calls, put_raises=None, existing=None):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return _CapturingTable(calls, put_raises, existing)

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


def test_is_pending_false_when_step_already_finished(monkeypatch):
    calls = []
    conflict = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    _patch_table(monkeypatch, calls, put_raises=conflict,
                 existing={"status": "completed"})

    assert worker._is_pending("youtube-slack", "notify", EVENT) is False


def test_is_pending_raises_leasebusy_while_lease_is_live(monkeypatch):
    calls = []
    conflict = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    _patch_table(monkeypatch, calls, put_raises=conflict,
                 existing={"status": "processing", "lease_until": 9**9})

    with pytest.raises(worker.LeaseBusy):
        worker._is_pending("youtube-slack", "notify", EVENT)


def test_mark_completed_sets_finished(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)

    worker._mark_completed("youtube-slack", "notify", EVENT)
    kwargs = calls[0][1]
    assert calls[0][0] == "update_item"
    assert kwargs["ExpressionAttributeValues"][":status"] == "completed"
    assert kwargs["ExpressionAttributeValues"][":finished"]
    assert ":output" not in kwargs["ExpressionAttributeValues"]


def test_mark_completed_records_filtered_status(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)

    worker._mark_completed("wf", "gate", EVENT, output={"filter": "stopped"},
                           status="filtered")
    kwargs = calls[0][1]
    assert kwargs["ExpressionAttributeValues"][":status"] == "filtered"
    assert kwargs["ExpressionAttributeValues"][":output"] == {"filter": "stopped"}


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


class FakeSes:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "ses-1"}


def test_notify_failure_emails_the_workflow_notify_list(monkeypatch):
    import src.dapier.engine.matching as matching

    calls = []
    _patch_table(monkeypatch, calls)
    monkeypatch.setattr(matching, "all_workflows",
                        lambda: [{"id": "wf-1", "notify": ["ops@example.test", "  ", "lead@example.test"]}])
    ses = FakeSes()
    exc = ValueError("Slack rejected message")
    exc.dapier_workflow = "wf-1"
    event = {"id": "evt-1", "connector": "email", "event": "message.received"}

    result = notify.notify_failure(exc, event, ses=ses)

    assert result["run_id"] == "wf-1:evt-1"
    sent = ses.calls[0]
    assert sent["Destination"]["ToAddresses"] == ["ops@example.test", "lead@example.test"]
    assert sent["Source"]
    assert "Run failed" in sent["Message"]["Subject"]["Data"]
    body = sent["Message"]["Body"]["Text"]["Data"]
    assert "workflow: wf-1" in body
    assert "run: wf-1:evt-1" in body
    assert "failing step error: Slack rejected message" in body
    item = calls[0][1]["Item"]
    assert item["execution_id"] == "wf-1:failure-notice:evt-1"
    assert item["run_id"] == "wf-1:evt-1#notice"
    assert item["kind"] == "failure-notice"
    assert item["status"] == "notified"


def test_notify_failure_defaults_to_the_operator_address_without_notify(monkeypatch):
    import src.dapier.engine.matching as matching

    calls = []
    _patch_table(monkeypatch, calls)
    monkeypatch.setattr(matching, "all_workflows", lambda: [{"id": "wf-1"}])
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")
    ses = FakeSes()
    exc = ValueError("boom")
    exc.dapier_workflow = "wf-1"

    result = notify.notify_failure(exc, {"id": "evt-1"}, ses=ses)

    # No `notify:` key no longer means silence: the operator is the default.
    assert result["to"] == ["ops@example.test"]
    sent = ses.calls[0]
    assert sent["Destination"]["ToAddresses"] == ["ops@example.test"]
    assert "Run failed" in sent["Message"]["Subject"]["Data"]


def test_notify_failure_explicit_empty_notify_opts_out(monkeypatch):
    import src.dapier.engine.matching as matching

    calls = []
    _patch_table(monkeypatch, calls)
    monkeypatch.setattr(matching, "all_workflows",
                        lambda: [{"id": "wf-1", "notify": []}])
    monkeypatch.setenv("DAPIER_EMAIL_SENDER", "ops@example.test")
    ses = FakeSes()
    exc = ValueError("boom")
    exc.dapier_workflow = "wf-1"

    assert notify.notify_failure(exc, {"id": "evt-1"}, ses=ses) is None
    assert ses.calls == []
    assert calls == []


def test_notify_failure_sends_at_most_once_per_run(monkeypatch):
    import src.dapier.engine.matching as matching

    calls = []
    conflict = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    _patch_table(monkeypatch, calls, put_raises=conflict)
    monkeypatch.setattr(matching, "all_workflows",
                        lambda: [{"id": "wf-1", "notify": ["ops@example.test"]}])
    ses = FakeSes()
    exc = ValueError("boom")
    exc.dapier_workflow = "wf-1"

    assert notify.notify_failure(exc, {"id": "evt-1"}, ses=ses) is None
    assert ses.calls == []


def test_notify_failure_ignores_untagged_errors_and_missing_payloads(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)
    assert notify.notify_failure(ValueError("boom"), {"id": "evt-1"}, ses=FakeSes()) is None
    tagged = ValueError("boom")
    tagged.dapier_workflow = "wf-1"
    assert notify.notify_failure(tagged, None, ses=FakeSes()) is None


def test_handler_notifies_on_failed_queue_records(monkeypatch):
    notified = []
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    monkeypatch.setattr(worker.inbox, "record", lambda event: None)
    monkeypatch.setattr(worker.inbox, "complete", lambda *a, **k: None)

    def boom(payload, **hooks):
        exc = ValueError("boom")
        exc.dapier_workflow = "wf-1"
        raise exc

    monkeypatch.setattr(worker, "execute", boom)
    event = {"Records": [{"messageId": "m1",
                          "body": json.dumps({"id": "evt-1", "connector": "email"})}]}

    result = worker.handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert notified == [{"id": "evt-1", "connector": "email"}]


def test_handler_records_inbox_outcome(monkeypatch):
    completed = []
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: None)
    monkeypatch.setattr(worker.inbox, "record", lambda event: "inbox-1")
    monkeypatch.setattr(worker.inbox, "complete",
                        lambda inbox_id, matched, error=None: completed.append((inbox_id, matched, error)))
    monkeypatch.setattr(worker, "execute", lambda payload, **hooks: ["wf-1"])

    event = {"Records": [{"messageId": "m1",
                          "body": json.dumps({"id": "evt-1", "connector": "email"})}]}
    result = worker.handler(event, None)

    assert result == {"batchItemFailures": []}
    assert completed == [("inbox-1", ["wf-1"], None)]


def test_handler_requeues_and_stays_quiet_on_lease_busy(monkeypatch):
    notified = []
    completed = []
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    monkeypatch.setattr(worker.inbox, "record", lambda event: "inbox-1")
    monkeypatch.setattr(worker.inbox, "complete",
                        lambda inbox_id, matched, error=None: completed.append((inbox_id, matched, error)))

    def busy(payload, **hooks):
        raise worker.LeaseBusy("still processing")

    monkeypatch.setattr(worker, "execute", busy)
    event = {"Records": [{"messageId": "m1",
                          "body": json.dumps({"id": "evt-1", "connector": "email"})}]}

    result = worker.handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert notified == []
    assert completed == [("inbox-1", None, "requeued: a delivery is still in flight")]


def test_handler_skips_notifications_when_the_payload_never_parses(monkeypatch):
    notified = []
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    monkeypatch.setattr(worker, "execute", lambda payload, **hooks: None)
    event = {"Records": [{"messageId": "m1", "body": "not json"}]}

    result = worker.handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert notified == [None]


def test_handler_notifies_and_reraises_for_failed_schedule_triggers(monkeypatch):
    notified = []
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    monkeypatch.setattr(worker, "execute", lambda payload, **hooks: (_ for _ in ()).throw(ValueError("boom")))

    with pytest.raises(ValueError):
        worker.handler({"trigger": "schedule", "schedule_id": "nightly"}, None)

    assert len(notified) == 1
    assert notified[0]["data"]["schedule"] == "nightly"


def test_handler_notifies_with_a_poll_event_on_poll_trigger_failure(monkeypatch):
    notified = []
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    import src.dapier.triggers.poll_triggers as poll_triggers
    monkeypatch.setattr(poll_triggers, "fire", lambda name: (_ for _ in ()).throw(RuntimeError("fetch failed")))

    with pytest.raises(RuntimeError):
        worker.handler({"trigger": "poll", "poll_id": "new-items"}, None)

    # The failure names the trigger instead of passing None: the operator
    # notify path can resolve a recipient and a subject from it.
    assert len(notified) == 1
    event = notified[0]
    assert event["connector"] == "poll"
    assert event["event"] == "poll.failed"
    assert event["data"]["poll"] == "new-items"
    assert event["id"]



class _FakeQueue:
    """Stands in for the SQS client: captures what the worker parks."""

    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": f"sqsm-{len(self.messages)}"}


class _FakeTime:
    """Replaces worker.time so tests can fast-forward to the resume moment."""

    def __init__(self, now=None):
        self.now = now if now is not None else time.time()

    def time(self):
        return self.now


def _stub_step_leases(monkeypatch, leases=None, completed=None):
    """Replace the DynamoDB-backed step hooks with recorders.

    The lease gate stays in the call path (it is asserted on); only the
    table writes are stubbed out.
    """
    leases = leases if leases is not None else []
    completed = completed if completed is not None else []
    monkeypatch.setattr(worker, "_is_pending",
                        lambda wf, aid, ev, kind=None, retry_attempt=None:
                        leases.append((wf, aid, kind)) or True)
    monkeypatch.setattr(worker, "_mark_completed",
                        lambda wf, aid, ev, **kw:
                        completed.append((aid, kw.get("status"))))
    monkeypatch.setattr(worker, "_release_action", lambda *a, **k: None)
    return leases, completed


PARKED_WORKFLOW = {
    "id": "wf-1", "enabled": True,
    "trigger": {"connector": "email", "event": "message.received", "filters": {}},
    "actions": [
        {"id": "post", "type": "webhook", "url": "https://example.test"},
        {"id": "pause", "type": "delay", "seconds": 61},
        {"id": "after", "type": "webhook", "url": "https://example.test"},
    ],
}


def test_enqueue_resume_parks_the_continuation_envelope(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    queue = _FakeQueue()
    resume_at = time.time() + 30.25
    segments = [{"steps": PARKED_WORKFLOW["actions"][2], "prefix": "", "scope": None}]

    chunk = worker._enqueue_resume(
        "wf-1", EVENT, resume_at, segments,
        {"pause": {"status": "delayed", "output": {}}},
        "pause", queue=queue,
    )

    assert chunk == 31  # ceil of the remaining wait
    sent = queue.messages[0]
    assert sent["QueueUrl"] == "https://sqs.test/events"
    assert sent["DelaySeconds"] == 31
    envelope = json.loads(sent["MessageBody"])["dapier_resume"]
    assert envelope["workflow_id"] == "wf-1"
    assert envelope["event"] == EVENT
    assert envelope["resume_at"] == resume_at  # absolute moment, not a countdown
    assert envelope["delay_action_id"] == "pause"
    assert envelope["run_id"] == "wf-1:evt-1"
    assert envelope["segments"] == segments
    assert envelope["step_outputs"]["pause"]["status"] == "delayed"


def test_enqueue_resume_caps_one_hop_at_nine_hundred_seconds(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    queue = _FakeQueue()

    chunk = worker._enqueue_resume(
        "wf-1", EVENT, time.time() + 5000, [], {}, "pause", queue=queue)

    assert chunk == worker.MAX_DELAY_QUEUE_SECONDS == 900
    assert queue.messages[0]["DelaySeconds"] == 900


def test_enqueue_resume_parks_a_due_moment_without_delay(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    queue = _FakeQueue()

    chunk = worker._enqueue_resume(
        "wf-1", EVENT, time.time() - 10, [], {}, "pause", queue=queue)

    assert chunk == 0
    assert queue.messages[0]["DelaySeconds"] == 0


def test_resume_run_rechains_a_wait_longer_than_one_hop(monkeypatch):
    """Waits over 900 s chain re-enqueues; the absolute resume_at stays exact."""
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    fake_time = _FakeTime()
    monkeypatch.setattr(worker, "time", fake_time)
    queue = _FakeQueue()
    resume = {
        "workflow_id": "wf-1", "event": EVENT,
        "resume_at": fake_time.now + 1800,  # two hops away
        "delay_action_id": "pause",
        "segments": [{"steps": [PARKED_WORKFLOW["actions"][2]]}],
        "step_outputs": {},
        "run_id": "wf-1:evt-1",
    }
    ran = []
    _stub_step_leases(monkeypatch)
    monkeypatch.setattr(worker, "_run_connector",
                        lambda action, event, workflow_id, steps=None:
                        ran.append(action["id"]) or {"ok": True})

    worker._resume_run(resume, queue=queue)  # hop 1: 900 s of the 1800 s wait
    assert ran == []
    assert queue.messages[0]["DelaySeconds"] == 900

    resent = json.loads(queue.messages[0]["MessageBody"])["dapier_resume"]
    assert resent["resume_at"] == resume["resume_at"]
    fake_time.now += 900

    worker._resume_run(resent, queue=queue)  # hop 2: still early, chain again
    assert queue.messages[1]["DelaySeconds"] == 900
    assert ran == []

    fake_time.now = resume["resume_at"] + 1
    worker._resume_run(resent, queue=queue)  # the moment has passed: no re-enqueue
    assert len(queue.messages) == 2


def test_resume_run_completes_the_remaining_steps_through_the_step_leases(monkeypatch):
    ran = []
    leases, completed = _stub_step_leases(monkeypatch)
    monkeypatch.setattr(worker, "_run_connector",
                        lambda action, event, workflow_id, steps=None:
                        ran.append(action["id"]) or {"ok": True})
    fake_time = _FakeTime()
    monkeypatch.setattr(worker, "time", fake_time)
    resume = {
        "workflow_id": "wf-1", "event": EVENT,
        "resume_at": fake_time.now - 5,  # due
        "delay_action_id": "pause",
        "segments": [{"steps": [PARKED_WORKFLOW["actions"][2]], "prefix": "", "scope": None}],
        "step_outputs": {"pause": {"status": "delayed",
                                   "output": {"suspended": True}}},
        "run_id": "wf-1:evt-1",
    }

    stop = worker._resume_run(resume)

    assert stop is None
    assert ran == ["after"]  # only the remainder; the pause never re-runs
    # The resumed step goes through the same conditional-write lease gate as
    # a fresh run's step.
    assert leases == [("wf-1", "after", "webhook")]
    assert ("after", "completed") in completed


def test_resume_run_with_nothing_to_run_is_dropped(monkeypatch):
    queue = _FakeQueue()
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")

    assert worker._resume_run({"workflow_id": "", "event": None}, queue=queue) is None
    assert worker._resume_run({"workflow_id": "wf-1", "event": "not-a-dict"}, queue=queue) is None
    assert queue.messages == []


def test_handler_resumes_a_parked_run_before_the_trigger_path(monkeypatch):
    resumed = []
    recorded = []
    monkeypatch.setattr(worker, "_resume_run", lambda resume: resumed.append(resume))
    monkeypatch.setattr(worker.inbox, "record", lambda event: recorded.append(event))
    envelope = {"workflow_id": "wf-1", "event": EVENT, "resume_at": 1.0,
                "segments": [], "step_outputs": {}, "run_id": "wf-1:evt-1"}
    body = json.dumps({"dapier_resume": envelope})

    result = worker.handler({"Records": [{"messageId": "m1", "body": body}]}, None)

    assert result == {"batchItemFailures": []}
    assert resumed == [envelope]  # the captured remainder replays as-is
    assert recorded == []  # never hit the trigger inbox or matching


def test_handler_parks_a_long_delay_and_the_resume_completes_the_run(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    monkeypatch.setattr(worker, "all_workflows", lambda: [PARKED_WORKFLOW])
    ran = []
    leases, completed = _stub_step_leases(monkeypatch)

    def runner(action, event, workflow_id, steps=None):
        ran.append(action["id"])
        return {"ok": True}

    monkeypatch.setattr(worker, "_run_connector", runner)
    monkeypatch.setattr(worker.inbox, "record", lambda event: "inbox-1")
    outcomes = []
    monkeypatch.setattr(worker.inbox, "complete",
                        lambda inbox_id, matched, error=None:
                        outcomes.append((inbox_id, matched, error)))
    queue = _FakeQueue()
    monkeypatch.setattr(worker, "_sqs", lambda: queue)

    # First delivery: the run parks at the delay step.
    result = worker.handler(
        {"Records": [{"messageId": "m1", "body": json.dumps(EVENT)}]}, None)

    assert result == {"batchItemFailures": []}
    assert ran == ["post"]  # the chain stopped at the pause
    assert [lease[1] for lease in leases] == ["post", "pause"]
    assert outcomes == [("inbox-1", ["wf-1"], None)]

    parked = json.loads(queue.messages[0]["MessageBody"])["dapier_resume"]
    assert queue.messages[0]["DelaySeconds"] == 61
    assert parked["workflow_id"] == "wf-1"
    assert parked["delay_action_id"] == "pause"
    assert parked["step_outputs"]["pause"]["status"] == "delayed"
    assert parked["segments"] == [{
        "steps": [PARKED_WORKFLOW["actions"][2]], "prefix": "", "scope": None}]

    # The continuation lands after the wait: the remainder replays to the
    # end, still through the same lease hooks, still one grouped run.
    monkeypatch.setattr(worker, "time", _FakeTime(now=parked["resume_at"] + 1))
    ran.clear()
    completed.clear()

    result = worker.handler(
        {"Records": [{"messageId": "m2",
                      "body": json.dumps({"dapier_resume": parked})}]}, None)

    assert result == {"batchItemFailures": []}
    assert ran == ["after"]
    assert ("after", "completed") in completed
    assert leases[-1] == ("wf-1", "after", "webhook")


def test_handler_fails_the_record_when_parking_cannot_enqueue(monkeypatch):
    fake_time = _FakeTime()
    monkeypatch.setattr(worker, "time", fake_time)
    monkeypatch.setattr(worker, "all_workflows", lambda: [PARKED_WORKFLOW])
    _stub_step_leases(monkeypatch)
    monkeypatch.setattr(worker, "_run_connector", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(worker.inbox, "record", lambda event: "inbox-1")
    outcomes = []
    monkeypatch.setattr(worker.inbox, "complete",
                        lambda inbox_id, matched, error=None:
                        outcomes.append((inbox_id, matched, error)))

    def broken_sqs():
        raise RuntimeError("sqs down")

    monkeypatch.setattr(worker, "_sqs", broken_sqs)

    result = worker.handler(
        {"Records": [{"messageId": "m1", "body": json.dumps(EVENT)}]}, None)

    # The park is load-bearing: without it the record fails so the event
    # re-runs (leases dedupe the steps that already ran, the delay suspends
    # again).
    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert outcomes[0][0] == "inbox-1"
    assert outcomes[0][1] is None
    assert outcomes[0][2] is not None


def test_handler_parks_a_suspended_schedule_run(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    queue = _FakeQueue()
    monkeypatch.setattr(worker, "_sqs", lambda: queue)
    suspension = worker.RunSuspended(time.time() + 61, {"suspended": True})
    suspension.workflow_id = "wf-1"
    suspension.event = dict(EVENT)
    monkeypatch.setattr(worker, "execute",
                        lambda payload, **hooks: (_ for _ in ()).throw(suspension))

    result = worker.handler({"trigger": "schedule", "schedule_id": "nightly"}, None)

    assert result == {"executed": "nightly"}  # not a failure
    sent = queue.messages[0]
    assert sent["DelaySeconds"] == 61
    envelope = json.loads(sent["MessageBody"])["dapier_resume"]
    assert envelope["workflow_id"] == "wf-1"
    assert envelope["segments"] == []


if __name__ == "__main__":
    unittest.main()
