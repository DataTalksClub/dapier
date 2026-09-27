"""Per-workflow retry policy: a failed action can wait and try again on the
workflow's dime (``retry: {attempts, backoff_seconds}``), with the attempt
counter riding an SQS message attribute so the event body stays pure data.

Composition: retry gates the worker's failure path — a retryable action
failure is re-enqueued with the policy's delay, and the existing failure
handling (notify, batchItemFailures/redrive) applies only once the attempts
are exhausted. Steps that handle their own failure (``on_fail``/``on_error``)
never reach the worker, so their author's policy wins; a workflow without the
``retry`` key behaves exactly as before.
"""
import json
import unittest
from unittest.mock import patch

import boto3
import pytest

from src.dapier.engine import worker
from src.dapier.engine.logic import run_chain


EVENT = {"id": "evt-1", "connector": "email", "event": "message.received",
         "source": "todo", "data": {"route": "todo"}}


class _CapturingTable:
    def __init__(self, calls):
        self._calls = calls

    def put_item(self, **kwargs):
        self._calls.append(("put_item", kwargs))

    def update_item(self, **kwargs):
        self._calls.append(("update_item", kwargs))


def _patch_table(monkeypatch, calls):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return _CapturingTable(calls)

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


class FakeQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": "sqsm-1"}


class FakeInbox:
    """Records the worker's inbox calls (the real one stays unconfigured)."""

    def __init__(self):
        self.recorded = []
        self.completed = []

    def record(self, event):
        self.recorded.append(event)
        return event.get("id")

    def complete(self, inbox_id, matched, *, error=None):
        self.completed.append((inbox_id, matched, error))


def _action_error(message="Slack rejected it", step_type="slack", step_id="post",
                  workflow_id="wf-1"):
    exc = ValueError(message)
    exc.dapier_workflow = workflow_id
    exc.dapier_step_type = step_type
    exc.dapier_step_id = step_id
    return exc


def test_retry_policy_defaults_bounds_and_opt_in():
    assert worker.retry_policy({"id": "wf-1"}) is None
    assert worker.retry_policy({"id": "wf-1", "retry": {}}) == {
        "attempts": 1, "backoff_seconds": 60}
    assert worker.retry_policy({"retry": {"attempts": 3, "backoff_seconds": 120}}) == {
        "attempts": 3, "backoff_seconds": 120}
    assert worker.retry_policy({"retry": {"attempts": 99, "backoff_seconds": 0}}) == {
        "attempts": 5, "backoff_seconds": 1}
    assert worker.retry_policy({"retry": {"attempts": "garbage"}}) == {
        "attempts": 1, "backoff_seconds": 60}
    assert worker.retry_policy({"retry": "yes"}) is None


def test_message_attempt_reads_the_sqs_attribute():
    record = {"messageAttributes": {"retry_attempt": {"stringValue": "2"}}}
    assert worker._message_attempt(record) == 2
    assert worker._message_attempt({}) == 0
    assert worker._message_attempt({"messageAttributes": {
        "retry_attempt": {"stringValue": "soon"}}}) == 0


def test_attempt_hooks_mark_step_records_on_later_attempts(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)

    first = worker._attempt_hooks(0)
    assert first["before_action"]("wf-1", "post", EVENT, "slack") is True
    assert "retry_attempt" not in calls[0][1]["Item"]

    calls.clear()
    retried = worker._attempt_hooks(2)
    assert retried["before_action"]("wf-1", "post", EVENT, "slack") is True
    assert calls[0][1]["Item"]["retry_attempt"] == 2
    assert calls[0][1]["Item"]["status"] == "processing"


def test_schedule_retry_reenqueues_with_delay_and_attribute(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    queue = FakeQueue()
    with patch("src.dapier.engine.worker.all_workflows",
               return_value=[{"id": "wf-1", "retry": {"attempts": 3, "backoff_seconds": 120}}]):
        scheduled = worker._schedule_retry(_action_error(), EVENT, 0, queue=queue)

    assert scheduled is True
    message = queue.messages[0]
    assert message["QueueUrl"] == "https://sqs.test/events"
    assert message["DelaySeconds"] == 120
    assert message["MessageAttributes"]["retry_attempt"] == {
        "DataType": "Number", "StringValue": "1"}
    assert json.loads(message["MessageBody"]) == EVENT  # the body stays pure trigger data
    annotated = calls[0][1]
    assert annotated["Key"]["execution_id"] == "wf-1:post:evt-1"
    assert annotated["ExpressionAttributeValues"][":attempt"] == 1


def test_schedule_retry_stops_after_the_last_attempt(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    queue = FakeQueue()
    with patch("src.dapier.engine.worker.all_workflows",
               return_value=[{"id": "wf-1", "retry": {"attempts": 3}}]):
        # Attempts are total tries: counters 0, 1, 2. The third failure exhausts.
        assert worker._schedule_retry(_action_error(), EVENT, 1, queue=queue) is True
        assert worker._schedule_retry(_action_error(), EVENT, 2, queue=queue) is False
    assert len(queue.messages) == 1
    assert [call[1]["ExpressionAttributeValues"][":attempt"] for call in calls] == [2]


def test_schedule_retry_ignores_logic_steps_untagged_and_nonoptins(monkeypatch):
    calls = []
    _patch_table(monkeypatch, calls)
    queue = FakeQueue()
    workflows = [{"id": "wf-1", "retry": {"attempts": 3}}, {"id": "wf-plain"}]
    with patch("src.dapier.engine.worker.all_workflows", return_value=workflows):
        # A logic-step failure is configuration; retrying cannot fix it.
        assert worker._schedule_retry(_action_error(step_type="delay", step_id="pause"),
                                      EVENT, 0, queue=queue) is False
        assert worker._schedule_retry(_action_error(step_type="for_each", step_id="each"),
                                      EVENT, 0, queue=queue) is False
        # A workflow without the retry key behaves exactly as before.
        assert worker._schedule_retry(_action_error(workflow_id="wf-plain"), EVENT, 0,
                                      queue=queue) is False
        # Untagged errors (the chain never reached a step) never retry.
        assert worker._schedule_retry(ValueError("boom"), EVENT, 0, queue=queue) is False
        assert worker._schedule_retry(_action_error(), None, 0, queue=queue) is False
        # A workflow unknown to the catalog (removed mid-flight) never retries.
        exc = _action_error()
        exc.dapier_workflow = "wf-gone"
        assert worker._schedule_retry(exc, EVENT, 0, queue=queue) is False
    assert queue.messages == []
    assert calls == []


def test_handler_retries_a_failed_action_without_batch_failure(monkeypatch):
    notified = []
    _patch_table(monkeypatch, [])
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    monkeypatch.setattr(worker, "inbox", FakeInbox())

    def boom(payload, **hooks):
        raise _action_error()

    monkeypatch.setattr(worker, "execute", boom)
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    with patch("src.dapier.engine.worker.all_workflows",
               return_value=[{"id": "wf-1", "retry": {"attempts": 3, "backoff_seconds": 30}}]):
        queue = FakeQueue()
        monkeypatch.setattr(worker, "_sqs", lambda: queue)
        event = {"Records": [{"messageId": "m1", "body": json.dumps(EVENT)}]}

        result = worker.handler(event, None)

    assert result == {"batchItemFailures": []}  # the retry owns the record now
    assert notified == []
    assert queue.messages[0]["DelaySeconds"] == 30


def test_handler_notes_the_pending_retry_on_the_inbox_row(monkeypatch):
    _patch_table(monkeypatch, [])
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    box = FakeInbox()
    monkeypatch.setattr(worker, "inbox", box)
    monkeypatch.setattr(worker, "execute",
                        lambda payload, **hooks: (_ for _ in ()).throw(_action_error()))
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: None)
    with patch("src.dapier.engine.worker.all_workflows",
               return_value=[{"id": "wf-1", "retry": {"attempts": 3}}]):
        queue = FakeQueue()
        monkeypatch.setattr(worker, "_sqs", lambda: queue)
        worker.handler({"Records": [{"messageId": "m1", "body": json.dumps(EVENT)}]}, None)

    assert box.recorded == [EVENT]
    assert box.completed == [("evt-1", None, "retry scheduled: a later "
                              "attempt is pending on the event queue")]


def test_handler_notifies_once_the_attempts_are_exhausted(monkeypatch):
    notified = []
    monkeypatch.setattr(worker, "inbox", FakeInbox())
    monkeypatch.setattr(worker, "execute",
                        lambda payload, **hooks: (_ for _ in ()).throw(_action_error()))
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    with patch("src.dapier.engine.worker.all_workflows",
               return_value=[{"id": "wf-1", "retry": {"attempts": 3}}]):
        queue = FakeQueue()
        monkeypatch.setattr(worker, "_sqs", lambda: queue)
        record = {"messageId": "m1", "body": json.dumps(EVENT),
                  "messageAttributes": {"retry_attempt": {"stringValue": "2"}}}

        result = worker.handler({"Records": [record]}, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert notified == [EVENT]
    assert queue.messages == []


def test_handler_mid_retry_success_records_the_attempt_and_stays_quiet(monkeypatch):
    """A later attempt that runs clean: no re-enqueue, no notify, and the
    step records carry the attempt number."""
    calls = []
    notified = []
    _patch_table(monkeypatch, calls)
    monkeypatch.setattr(worker, "inbox", FakeInbox())
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    seen_hooks = {}
    monkeypatch.setattr(
        worker, "execute",
        lambda payload, **hooks: seen_hooks.update(hooks) or ["wf-1"])
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    queue = FakeQueue()
    monkeypatch.setattr(worker, "_sqs", lambda: queue)
    record = {"messageId": "m1", "body": json.dumps(EVENT),
              "messageAttributes": {"retry_attempt": {"stringValue": "2"}}}

    assert worker.handler({"Records": [record]}, None) == {"batchItemFailures": []}
    assert notified == []
    assert queue.messages == []
    assert seen_hooks["before_action"]("wf-1", "post", EVENT, "slack") is True
    puts = [kwargs for name, kwargs in calls if name == "put_item"]
    assert puts[0]["Item"]["retry_attempt"] == 2


def test_handler_without_a_retry_key_keeps_todays_behavior(monkeypatch):
    notified = []
    monkeypatch.setattr(worker, "inbox", FakeInbox())
    monkeypatch.setattr(worker, "execute",
                        lambda payload, **hooks: (_ for _ in ()).throw(_action_error()))
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    with patch("src.dapier.engine.worker.all_workflows", return_value=[{"id": "wf-1"}]):
        queue = FakeQueue()
        monkeypatch.setattr(worker, "_sqs", lambda: queue)
        result = worker.handler(
            {"Records": [{"messageId": "m1", "body": json.dumps(EVENT)}]}, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
    assert notified == [EVENT]
    assert queue.messages == []


def test_handler_retries_a_failed_schedule_fire(monkeypatch):
    notified = []
    _patch_table(monkeypatch, [])
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    monkeypatch.setattr(worker, "inbox", FakeInbox())

    def boom(payload, **hooks):
        raise _action_error()

    monkeypatch.setattr(worker, "execute", boom)
    monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
    with patch("src.dapier.engine.worker.all_workflows",
               return_value=[{"id": "wf-1", "retry": {"attempts": 2, "backoff_seconds": 45}}]):
        queue = FakeQueue()
        monkeypatch.setattr(worker, "_sqs", lambda: queue)
        result = worker.handler({"trigger": "schedule", "schedule_id": "sched-1"}, None)

    assert result == {"executed": "sched-1"}  # the retry owns the failure: no raise
    assert notified == []
    assert queue.messages[0]["DelaySeconds"] == 45


class LogicTaggingTests(unittest.TestCase):
    """The exception tags that tell a retryable action failure from a
    logic-step config error, including through nested chains."""

    def _run(self, steps, run_action, data=None):
        event = {"id": "evt-1", "data": {} if data is None else data}
        try:
            run_chain("wf-1", steps, event, run_action)
        except Exception as exc:
            return exc
        return None

    def test_action_failure_tags_the_action_step(self):
        def run_action(step, event, workflow_id, steps=None):
            if step["type"] == "slack":
                raise RuntimeError("rejected")
            return {}

        exc = self._run([{"id": "send", "type": "slack"}], run_action)
        assert exc.dapier_workflow == "wf-1"
        assert exc.dapier_step_id == "send"
        assert exc.dapier_step_type == "slack"

    def test_logic_failure_tags_the_logic_step(self):
        exc = self._run([{"id": "pause", "type": "delay"}, {"id": "send", "type": "slack"}],
                        lambda step, event, workflow_id, steps=None: {})
        assert exc.dapier_step_id == "pause"
        assert exc.dapier_step_type == "delay"

    def test_inner_action_failure_keeps_its_tags_through_a_loop(self):
        def run_action(step, event, workflow_id, steps=None):
            if step["type"] == "slack":
                raise RuntimeError("rejected")
            return {}

        event = {"id": "evt-1", "data": {"items": [{"name": "a"}, {"name": "b"}]}}
        exc = self._run([
            {"id": "loop", "type": "for_each", "list": "items", "item": "item",
             "actions": [{"id": "send", "type": "slack"}]},
        ], run_action, data=event["data"])
        assert exc.dapier_step_id == "loop[0].send"
        assert exc.dapier_step_type == "slack"

    def test_retry_never_touches_a_step_that_handles_its_own_failure(self):
        """``on_fail: continue`` absorbs the failure at the step: the chain
        completes, so nothing propagates to the worker's retry decision."""
        calls = []

        def run_action(step, event, workflow_id, steps=None):
            calls.append(step["id"])
            if step["id"] == "boom":
                raise RuntimeError("rejected")
            return {}

        stop = run_chain(
            "wf-1",
            [
                {"id": "boom", "type": "slack", "on_fail": "continue"},
                {"id": "post", "type": "slack"},
            ],
            {"id": "evt-1", "data": {}}, run_action,
        )
        assert stop is None
        assert calls == ["boom", "post"]


class RunHistoryTests(unittest.TestCase):
    """Run history shows the retry: step records carry the attempt number and
    the run summary rolls the highest attempt up."""

    def _step(self, retry_attempt=None):
        item = {"execution_id": "wf-1:post:evt-1", "run_id": "wf-1:evt-1",
                "workflow_id": "wf-1", "action_id": "post", "status": "failed",
                "started_at": "2026-09-25T10:00:00+00:00"}
        if retry_attempt:
            item["retry_attempt"] = retry_attempt
        return item

    def test_summary_rolls_up_the_attempt_count_and_steps_carry_it(self):
        from src.dapier.api import runs

        monkey_items = [self._step(3)]
        summary = runs.run_summary("wf-1:evt-1", monkey_items)
        assert summary["attempts"] == 3

        view = runs._step_view(monkey_items[0])
        assert view["retry_attempt"] == 3

    def test_first_pass_runs_report_no_attempts(self):
        from src.dapier.api import runs

        summary = runs.run_summary("wf-1:evt-1", [self._step()])
        assert summary["attempts"] is None
        assert runs._step_view(self._step())["retry_attempt"] is None


if __name__ == "__main__":
    pytest.main([__file__])
