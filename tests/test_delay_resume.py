"""Real delays: park-and-resume for waits past one invocation (gap G2).

A delay past ``logic.MAX_DELAY_SECONDS`` suspends the chain (``RunSuspended``,
engine.logic); the worker catches it and parks the run on the event queue: a
``dapier_resume`` envelope with SQS ``DelaySeconds = min(remaining, 900)``.
SQS cannot delay longer than 900s, so longer waits chain — every arrival
that lands before ``resume_at`` re-enqueues for the remainder, and the
captured segments only replay once the moment has passed. Short delays keep
the in-process sleep, and nothing parks outside the worker (dry-runs and
``wf test`` go through engine.execute, which never suspends a run).
"""
import json
import time
import unittest
from unittest.mock import patch

import pytest

from src.dapier.engine import logic, worker
from botocore.exceptions import ClientError


EVENT = {
    "id": "evt-1",
    "connector": "email",
    "event": "message.received",
    "data": {"route": "invoice"},
}

PAUSE = {"id": "pause", "type": "delay", "seconds": 90}
POST = {"id": "post", "type": "webhook", "url": "https://example.test"}
FINAL = {"id": "final", "type": "webhook", "url": "https://example.test/final"}


class Hooks:
    """Record the telemetry calls the chain makes, like the worker's ledger."""

    def __init__(self, pending=True):
        self.calls = []
        self.pending = pending

    def before(self, workflow_id, action_id, event, action_type):
        self.calls.append(("before", action_id, action_type))
        return self.pending

    def after(self, workflow_id, action_id, event, **kwargs):
        self.calls.append(("after", action_id, kwargs.get("status"), kwargs.get("output")))

    def error(self, workflow_id, action_id, event, exc, **kwargs):
        self.calls.append(("error", action_id, str(exc)))


def run_chain(steps, *, data=None, hooks=None, run_action=None):
    hooks = hooks or Hooks()
    event = {**EVENT, "data": EVENT["data"] if data is None else data}
    stop = logic.run_chain(
        "wf-1", steps, event,
        run_action or (lambda action, event, workflow_id, steps=None: {"ok": True}),
        before_action=hooks.before, after_action=hooks.after,
        on_action_error=hooks.error,
    )
    return stop, hooks


def a_suspension(resume_at=None, **overrides):
    """A filled RunSuspended, as the worker's execute leaves it."""
    susp = logic.RunSuspended(resume_at if resume_at is not None else time.time() + 90,
                              {"delay_seconds": 90, "suspended": True,
                               "resume_at": None})
    susp.output["resume_at"] = logic._iso(susp.resume_at)
    susp.workflow_id = "wf-1"
    susp.event = dict(EVENT)
    susp.delay_action_id = "pause"
    susp.paused_ids = ["pause"]
    susp.segments = [{"steps": [POST], "prefix": "", "scope": None}]
    susp.step_outputs = {"pause": {"status": "delayed", "output": dict(susp.output)}}
    for key, value in overrides.items():
        setattr(susp, key, value)
    return susp


class FakeSqs:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": f"sqm-{len(self.messages)}"}


class _CapturingTable:
    def __init__(self, calls):
        self._calls = calls

    def put_item(self, **kwargs):
        self._calls.append(("put_item", kwargs))

    def update_item(self, **kwargs):
        self._calls.append(("update_item", kwargs))

    def get_item(self, **kwargs):
        self._calls.append(("get_item", kwargs))
        return {}


def _patch_table(monkeypatch, calls):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return _CapturingTable(calls)

    import boto3
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


class SuspendTests(unittest.TestCase):
    """The engine side: short delays sleep, long ones suspend the chain."""

    def test_short_delay_sleeps_inline(self):
        with patch("src.dapier.engine.logic.time") as fake_time:
            fake_time.monotonic.side_effect = [0.0, 0.05]
            fake_time.time.return_value = 1000.0
            stop, hooks = run_chain([{"id": "pause", "type": "delay", "seconds": 30}])

        assert stop is None
        fake_time.sleep.assert_called_once_with(30)
        assert ("after", "pause", "completed",
                {"delay_seconds": 30, "slept_seconds": 30}) in hooks.calls

    def test_long_delay_suspends_with_the_chain_remainder(self):
        runner_calls = []
        hooks = Hooks()
        with pytest.raises(logic.RunSuspended) as excinfo:
            run_chain([PAUSE, POST], hooks=hooks,
                      run_action=lambda action, event, workflow_id, steps=None:
                          runner_calls.append(action["id"]) or {})

        susp = excinfo.value
        assert runner_calls == []  # nothing after the pause ran
        assert susp.segments == [{"steps": [POST], "prefix": "", "scope": None}]
        assert susp.output["suspended"] is True
        assert susp.resume_at > time.time()
        # The pause itself is visible in run history as ``delayed``.
        assert ("after", "pause", "delayed", susp.output) in hooks.calls

    def test_branch_delay_suspends_innermost_first(self):
        steps = [{"id": "route", "type": "condition", "field": "route",
                  "operator": "equals", "value": "invoice",
                  "then": [PAUSE, POST]},
                 FINAL]
        with pytest.raises(logic.RunSuspended) as excinfo:
            run_chain(steps)

        assert excinfo.value.segments == [
            {"steps": [POST], "prefix": "route.then", "scope": None},
            {"steps": [FINAL], "prefix": "", "scope": None},
        ]
        # Every frame the unwind passed through recorded itself for the
        # resume's close-out; the delay step owns ``delay_action_id``.
        assert excinfo.value.paused_ids == ["route.then.pause", "route"]
        assert excinfo.value.delay_action_id == "route.then.pause"

    def test_loop_delay_suspends_with_the_loop_position(self):
        step = {"id": "each", "type": "for_each", "list": "attachments",
                "actions": [PAUSE, POST]}
        with pytest.raises(logic.RunSuspended) as excinfo:
            run_chain([step, FINAL],
                      data={"attachments": [{"filename": "a.pdf"}, {"filename": "b.pdf"}]})

        segments = excinfo.value.segments
        assert segments[0]["steps"] == [POST]
        assert segments[0]["prefix"] == "each[0]"
        assert segments[0]["scope"]["item_index"] == 0
        assert segments[1]["loop"]["action_id"] == "each"
        assert segments[1]["loop"]["from"] == 1
        assert segments[2] == {"steps": [FINAL], "prefix": "", "scope": None}


class ResumeChainTests(unittest.TestCase):
    """Replaying the captured segments once the wait is over."""

    def replay(self, segments, *, data=None, run_action=None, hooks=None,
               step_outputs=None):
        hooks = hooks or Hooks()
        event = dict(EVENT)
        if data is not None:
            event["data"] = data
        stop = logic.resume_chain(
            "wf-1", segments, event,
            run_action or (lambda action, event, workflow_id, steps=None: {"ok": True}),
            before_action=hooks.before, after_action=hooks.after,
            on_action_error=hooks.error, step_outputs=step_outputs,
        )
        return stop, hooks

    def test_replays_segments_in_order_with_their_prefixes(self):
        ran = []
        stop, hooks = self.replay(
            [{"steps": [POST], "prefix": "route.then", "scope": None},
             {"steps": [FINAL], "prefix": ""}],
            run_action=lambda action, event, workflow_id, steps=None:
                ran.append(action["id"]) or {"ok": True},
        )

        assert stop is None
        assert ran == ["post", "final"]
        assert ("before", "route.then.post", "webhook") in hooks.calls
        assert ("before", "final", "webhook") in hooks.calls

    def test_carries_the_parked_runs_step_outputs_forward(self):
        outputs = {"pause": {"status": "delayed", "output": {"delay_seconds": 90}}}
        seen = {}
        self.replay([{"steps": [POST]}],
                    run_action=lambda action, event, workflow_id, steps=None:
                        seen.update(steps=steps) or {},
                    step_outputs=outputs)

        assert seen["steps"]["pause"]["status"] == "delayed"

    def test_a_filter_stops_the_segments_behind_it(self):
        ran = []
        stop, _hooks = self.replay(
            [{"steps": [{"id": "gate", "type": "filter", "field": "route",
                         "operator": "equals", "value": "receipt"}, POST]},
             {"steps": [FINAL]}],
            run_action=lambda action, event, workflow_id, steps=None:
                ran.append(action["id"]) or {},
        )

        assert stop == "filtered"
        assert ran == []

    def test_a_delay_in_the_remainder_suspends_again(self):
        with pytest.raises(logic.RunSuspended) as excinfo:
            self.replay([{"steps": [{"id": "pause2", "type": "delay", "seconds": 120}, POST]}])

        assert excinfo.value.segments == [{"steps": [POST], "prefix": "", "scope": None}]

    def test_a_delay_in_the_remainder_keeps_the_segments_behind_it(self):
        # The replay paused in the first segment: the outer tail must ride
        # along in the new suspension (passed through verbatim), or it would
        # silently never run.
        with pytest.raises(logic.RunSuspended) as excinfo:
            self.replay([{"steps": [{"id": "pause2", "type": "delay", "seconds": 120}, POST]},
                         {"steps": [FINAL]}])

        assert excinfo.value.segments == [
            {"steps": [POST], "prefix": "", "scope": None},
            {"steps": [FINAL]},
        ]

    def test_resumes_a_loop_from_its_position(self):
        ran = []
        step = {"id": "each", "type": "for_each", "list": "attachments",
                "actions": [{"id": "upload", "type": "webhook",
                             "url": "https://example.test/{item.filename}"}]}
        self.replay(
            [{"loop": {"action_id": "each", "step": step, "from": 1,
                       "scope": None, "item_var": "item", "cap": 100}}],
            data={"attachments": [{"filename": "a.pdf"}, {"filename": "b.pdf"}]},
            run_action=lambda action, event, workflow_id, steps=None:
                ran.append(action["url"]) or {},
        )

        assert ran == ["https://example.test/b.pdf"]  # item 0 ran before the park


class TestPark:
    """The worker side: a suspension leaves the continuation on the queue."""

    def test_park_enqueues_the_resume_envelope(self, monkeypatch):
        monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
        sqs = FakeSqs()
        susp = a_suspension()

        chunk = worker._park_suspension(susp, queue=sqs)

        assert chunk == 90
        message = sqs.messages[0]
        assert message["QueueUrl"] == "https://sqs.test/events"
        assert message["DelaySeconds"] == 90
        resume = json.loads(message["MessageBody"])["dapier_resume"]
        assert resume["workflow_id"] == "wf-1"
        assert resume["event"]["id"] == "evt-1"
        assert resume["run_id"] == "wf-1:evt-1"
        assert resume["delay_action_id"] == "pause"
        assert resume["paused_ids"] == ["pause"]
        assert resume["resume_at"] == susp.resume_at
        assert resume["segments"] == [{"steps": [POST], "prefix": "", "scope": None}]
        assert resume["step_outputs"]["pause"]["status"] == "delayed"

    def test_park_caps_one_hop_at_900_seconds(self, monkeypatch):
        monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
        sqs = FakeSqs()
        susp = a_suspension(resume_at=time.time() + 2000)

        chunk = worker._park_suspension(susp, queue=sqs)

        assert chunk == 900
        resume = json.loads(sqs.messages[0]["MessageBody"])["dapier_resume"]
        # The envelope keeps the absolute moment, so later hops stay exact.
        assert resume["resume_at"] == susp.resume_at

    def test_worker_execute_tags_the_suspension_with_workflow_and_event(self, monkeypatch):
        workflow = {"id": "wf-delay", "enabled": True,
                    "trigger": {"connector": "email", "event": "message.received",
                                "filters": {}},
                    "actions": [PAUSE, POST]}
        monkeypatch.setattr(worker, "all_workflows", lambda: [workflow])
        closed_out = []

        with pytest.raises(logic.RunSuspended) as excinfo:
            worker.execute(dict(EVENT),
                           after_action=lambda *a, **k: closed_out.append(k.get("status")))

        susp = excinfo.value
        # The pause itself closed out ``delayed`` (run history shows it);
        # the webhook after it never ran.
        assert closed_out == ["delayed"]
        assert susp.workflow_id == "wf-delay"
        assert susp.event["id"] == "evt-1"
        assert susp.segments == [{"steps": [POST], "prefix": "", "scope": None}]


class TestResumeRun:
    """Continuing a parked run from its envelope."""

    def envelope(self, resume_at=None):
        resume = json.loads(json.dumps({
            "workflow_id": "wf-1",
            "event": dict(EVENT),
            "resume_at": resume_at if resume_at is not None else time.time() - 1,
            "delay_action_id": "pause",
            "paused_ids": ["pause"],
            "segments": [{"steps": [POST], "prefix": "", "scope": None}],
            "step_outputs": {"pause": {"status": "delayed",
                                       "output": {"delay_seconds": 90}}},
            "run_id": "wf-1:evt-1",
        }))
        return resume

    def test_resume_before_the_moment_reenqueues_without_running(self, monkeypatch):
        monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
        sqs = FakeSqs()
        resume = self.envelope(resume_at=time.time() + 2000)

        chunk = worker._resume_run(resume, queue=sqs)

        assert chunk == 900  # chained hop: 900 now, the rest on the next arrival
        hop = json.loads(sqs.messages[0]["MessageBody"])["dapier_resume"]
        assert hop["resume_at"] == resume["resume_at"]
        assert hop["segments"] == resume["segments"]

    def test_resume_after_the_moment_replays_the_segments(self, monkeypatch):
        calls = []
        _patch_table(monkeypatch, calls)
        ran = []
        monkeypatch.setattr(worker, "_run_connector",
                            lambda action, event, workflow_id, steps=None:
                                ran.append((action["id"], dict(steps))) or {"status": 200})

        worker._resume_run(self.envelope(), queue=FakeSqs())

        assert [steps for _action_id, steps in ran] == [
            {"pause": {"status": "delayed", "output": {"delay_seconds": 90}}}]
        completions = [kwargs for kind, kwargs in calls
                       if kind == "update_item"
                       and kwargs["ExpressionAttributeValues"][":status"] == "completed"]
        # One close-out for the pause itself (the resume arrived, the wait is
        # over) and one for the resumed webhook step.
        assert len(completions) == 2
        assert [kwargs["Key"]["execution_id"] for kwargs in completions] == \
            ["wf-1:pause:evt-1", "wf-1:post:evt-1"]

    def test_resume_closes_every_paused_step_out_completed(self, monkeypatch):
        calls = []
        _patch_table(monkeypatch, calls)
        monkeypatch.setattr(worker, "_run_connector",
                            lambda action, event, workflow_id, steps=None: {"ok": True})
        resume = self.envelope()
        resume["paused_ids"] = ["route.then.pause", "route"]

        worker._resume_run(resume, queue=FakeSqs())

        updated = [kwargs["Key"]["execution_id"] for kind, kwargs in calls
                   if kind == "update_item"
                   and kwargs["ExpressionAttributeValues"][":status"] == "completed"]
        # The branch wrappers the unwind carried the pause through close out
        # completed too — run history no longer reads ``delayed``.
        assert "wf-1:route.then.pause:evt-1" in updated
        assert "wf-1:route:evt-1" in updated

    def test_resume_never_touches_the_table_before_the_moment(self, monkeypatch):
        monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
        calls = []
        _patch_table(monkeypatch, calls)
        sqs = FakeSqs()

        worker._resume_run(self.envelope(resume_at=time.time() + 5000), queue=sqs)

        assert calls == []  # nothing ran early; the wait is still parked
        assert len(sqs.messages) == 1

    def test_resume_of_an_empty_envelope_is_dropped_quietly(self, monkeypatch):
        calls = []
        _patch_table(monkeypatch, calls)

        assert worker._resume_run({"workflow_id": "", "event": {}, "segments": []}) is None
        assert worker._resume_run({"workflow_id": "wf-1", "event": "not-a-dict"}) is None

    def test_a_further_delay_in_the_remainder_reparks(self, monkeypatch):
        monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
        calls = []
        _patch_table(monkeypatch, calls)
        sqs = FakeSqs()
        resume = self.envelope()
        resume["segments"] = [{"steps": [{"id": "pause2", "type": "delay",
                                          "seconds": 120}, POST]}]

        with pytest.raises(logic.RunSuspended) as excinfo:
            worker._resume_run(resume, queue=sqs)

        susp = excinfo.value
        assert susp.workflow_id == "wf-1"
        assert susp.event["id"] == "evt-1"
        assert susp.segments == [{"steps": [POST], "prefix": "", "scope": None}]
        # The handler's RunSuspended path parks this just like a fresh run.
        assert worker._park_suspension(susp, queue=sqs) == 120


class TestHandlerPark:
    """The queue handler: parking is quiet, resumes bypass the inbox."""

    def test_handler_parks_a_suspended_run_quietly(self, monkeypatch):
        monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
        notified = []
        completed = []
        sqs = FakeSqs()
        monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
        monkeypatch.setattr(worker.inbox, "record", lambda event: "inbox-1")
        monkeypatch.setattr(worker.inbox, "complete",
                            lambda inbox_id, matched, error=None: completed.append((inbox_id, matched, error)))
        monkeypatch.setattr(worker, "execute",
                            lambda payload, **hooks: (_ for _ in ()).throw(a_suspension()))
        monkeypatch.setattr(worker, "_sqs", lambda: sqs)
        event = {"Records": [{"messageId": "m1",
                              "body": json.dumps({"id": "evt-1", "connector": "email"})}]}

        result = worker.handler(event, None)

        assert result == {"batchItemFailures": []}
        assert notified == []  # a parked run is not a failure
        assert completed == [("inbox-1", ["wf-1"], None)]
        assert sqs.messages[0]["DelaySeconds"] == 90

    def test_handler_parks_a_suspended_schedule_run(self, monkeypatch):
        monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
        sqs = FakeSqs()
        monkeypatch.setattr(worker, "execute",
                            lambda payload, **hooks: (_ for _ in ()).throw(a_suspension()))
        monkeypatch.setattr(worker, "_sqs", lambda: sqs)

        result = worker.handler({"trigger": "schedule", "schedule_id": "nightly"}, None)

        assert result == {"executed": "nightly"}
        assert len(sqs.messages) == 1

    def test_handler_fails_the_record_when_parking_cannot_enqueue(self, monkeypatch):
        notified = []
        completed = []
        park_error = RuntimeError("queue down")

        def broken_park(susp, **kwargs):
            raise park_error

        monkeypatch.setattr(worker, "notify_failure", lambda exc, event: notified.append(event))
        monkeypatch.setattr(worker.inbox, "record", lambda event: "inbox-1")
        monkeypatch.setattr(worker.inbox, "complete",
                            lambda inbox_id, matched, error=None: completed.append((inbox_id, matched, error)))
        monkeypatch.setattr(worker, "execute",
                            lambda payload, **hooks: (_ for _ in ()).throw(a_suspension()))
        monkeypatch.setattr(worker, "_park_suspension", broken_park)
        event = {"Records": [{"messageId": "m1",
                              "body": json.dumps({"id": "evt-1", "connector": "email"})}]}

        result = worker.handler(event, None)

        # The record goes back on the queue: the redelivery re-runs the chain
        # (leases dedupe the steps that already ran) and parks again.
        assert result == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
        assert notified == []
        assert completed == [("inbox-1", None, park_error)]

    def test_handler_runs_a_resume_envelope_without_the_inbox(self, monkeypatch):
        resumed = []
        recorded = []
        completed = []
        monkeypatch.setattr(worker, "_resume_run", lambda resume, **k: resumed.append(resume))
        monkeypatch.setattr(worker.inbox, "record", lambda event: recorded.append(event))
        monkeypatch.setattr(worker.inbox, "complete",
                            lambda inbox_id, matched, error=None: completed.append((inbox_id, matched)))
        envelope = {"workflow_id": "wf-1", "event": dict(EVENT),
                    "resume_at": time.time() - 1,
                    "segments": [{"steps": [POST], "prefix": "", "scope": None}]}
        event = {"Records": [{"messageId": "m1",
                              "body": json.dumps({"dapier_resume": envelope})}]}

        result = worker.handler(event, None)

        assert result == {"batchItemFailures": []}
        assert resumed == [envelope]
        assert recorded == [] and completed == []  # not a trigger event


if __name__ == "__main__":
    pytest.main([__file__])
