"""Replay-from-failed-step: a run retried at one chosen step onward.

``api_replay(run_id, from_step=...)`` publishes the worker's own resume
envelope (``{"dapier_resume": ...}``, the delay park-and-continue path)
rebuilt from run history: the workflow definition from the chosen step
onward as the resume segments, every earlier recorded step seeded into
``step_outputs`` (so ``{steps.<id>.output.*}`` templates resolve) and
listed as ``reused_steps``. The worker replays the remainder through the
same step leases as any resume and records the earlier steps as
``reused`` — they did not run again. Without ``from_step`` the replay is
unchanged: the plain trigger event goes back on the queue.
"""
import json
import time
import uuid

import boto3
import pytest

from dapier_cli import commands, main
from src.dapier.api import runs
from src.dapier.engine import worker


RUN_ID = "wf-1:evt-1"


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path



FIND_DEF = {"id": "find", "type": "http_request", "url": "https://x.invalid/search"}
POST_DEF = {"id": "post", "type": "webhook", "url": "https://x.invalid/post"}
NOTIFY_DEF = {"id": "notify", "type": "slack", "channel": "#ops"}
WORKFLOW = {"id": "wf-1", "enabled": True,
            "actions": [FIND_DEF, POST_DEF, NOTIFY_DEF]}


def history():
    """A failed run: find completed, post failed, notify never reached."""
    return [
        {"execution_id": "wf-1:find:evt-1", "run_id": RUN_ID,
         "workflow_id": "wf-1", "action_id": "find", "action_type": "http_request",
         "connector": "email", "event_type": "message.received", "status": "completed",
         "started_at": "2026-09-25T10:00:00+00:00",
         "finished_at": "2026-09-25T10:00:01+00:00",
         "input": {"subject": "invoice", "route": "find"},
         "output": {"rows": 3, "match": "INV-9001"}},
        {"execution_id": "wf-1:post:evt-1", "run_id": RUN_ID,
         "workflow_id": "wf-1", "action_id": "post", "action_type": "webhook",
         "connector": "email", "event_type": "message.received", "status": "failed",
         "started_at": "2026-09-25T10:00:01+00:00",
         "finished_at": "2026-09-25T10:00:02+00:00",
         "input": {"subject": "invoice", "route": "post"},
         "output": None, "error": "webhook returned HTTP 500"},
    ]


class FakeExecTable:
    """api_get's GSI miss falls back to the ledger scan; answers from items."""

    def __init__(self, items):
        self.items = items

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def query(self, **kwargs):
        values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
        wanted = values[0] if values else None
        return {"Items": [item for item in self.items if item.get("run_id") == wanted]}


class RecordingExecTable:
    """The worker's ledger: captures writes, like test_delay_resume's."""

    def __init__(self, calls):
        self.calls = calls

    def put_item(self, **kwargs):
        self.calls.append(("put", kwargs))

    def update_item(self, **kwargs):
        self.calls.append(("update", kwargs))

    def get_item(self, **kwargs):
        return {}

    def query(self, **kwargs):
        return {"Items": []}


class FakeSqs:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": f"sqm-{len(self.messages)}"}


def _configure_runs_read(monkeypatch, items):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return FakeExecTable(items)

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def _configure_worker(monkeypatch, calls, all_workflows=None):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return RecordingExecTable(calls)

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(worker, "all_workflows",
                        lambda: [WORKFLOW] if all_workflows is None else all_workflows)


def replay(monkeypatch, items=None, from_step=None):
    """api_replay against a fake queue; returns (response, captured messages)."""
    _configure_runs_read(monkeypatch, history() if items is None else items)
    monkeypatch.setattr(runs, "_workflows_now", lambda: [WORKFLOW])
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    sqs = FakeSqs()
    monkeypatch.setattr(runs, "_queue", lambda: sqs)

    status, payload = runs.api_replay(RUN_ID, from_step=from_step, queue=sqs)
    return (status, payload), sqs.messages


def test_replay_from_step_publishes_a_resume_envelope(monkeypatch):
    (status, payload), messages = replay(monkeypatch, from_step="post")

    assert status == 202
    assert payload["accepted"] is True
    assert payload["from_step"] == "post"
    assert payload["run_id"].startswith("wf-1:replay-")
    assert len(messages) == 1
    message = json.loads(messages[0]["MessageBody"])
    assert "connector" not in message  # not a trigger event — a resume
    resume = message["dapier_resume"]
    assert resume["workflow_id"] == "wf-1"
    assert resume["resume_at"] <= time.time() + 1  # due immediately
    assert resume["paused_ids"] == [] and resume["delay_action_id"] is None
    # Everything from the chosen step onward is the chain to re-execute.
    assert resume["segments"] == [{"steps": [POST_DEF, NOTIFY_DEF],
                                   "prefix": "", "scope": None}]
    # The steps before it ride along with their recorded outputs.
    assert resume["step_outputs"] == {
        "find": {"status": "completed", "output": {"rows": 3, "match": "INV-9001"}}}
    assert [step["action_id"] for step in resume["reused_steps"]] == ["find"]
    assert resume["run_id"] == payload["run_id"]


def test_replay_from_first_step_reuses_nothing(monkeypatch):
    (status, payload), messages = replay(monkeypatch, from_step="find")

    assert status == 202
    resume = json.loads(messages[0]["MessageBody"])["dapier_resume"]
    assert resume["segments"][0]["steps"] == [FIND_DEF, POST_DEF, NOTIFY_DEF]
    assert resume["step_outputs"] == {}
    assert resume["reused_steps"] == []


def test_replay_from_unknown_step_is_404(monkeypatch):
    (status, payload), messages = replay(monkeypatch, from_step="no-such-step")

    assert status == 404
    assert "no-such-step" in payload["error"]
    assert messages == []  # nothing published


def test_replay_from_nested_step_id_is_404(monkeypatch):
    # Only top-level steps can start a chain; a branch-inner id cannot.
    (status, payload), messages = replay(monkeypatch, from_step="branch.post")

    assert status == 404
    assert messages == []


def test_replay_from_a_step_nested_in_the_definition_is_409(monkeypatch):
    # The id names a real step (it sits inside a branch now), but only
    # top-level steps can start a chain: a conflict, not a miss.
    branched = {"id": "wf-1", "enabled": True,
                "actions": [{"id": "route", "type": "condition",
                             "field": "route", "operator": "equals",
                             "value": "invoice", "then": [POST_DEF]}]}
    _configure_runs_read(monkeypatch, history())
    monkeypatch.setattr(runs, "_workflows_now", lambda: [branched])
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    sqs = FakeSqs()
    monkeypatch.setattr(runs, "_queue", lambda: sqs)

    status, payload = runs.api_replay(RUN_ID, from_step="post", queue=sqs)

    assert status == 409
    assert "not a top-level step" in payload["error"]
    assert sqs.messages == []


def test_whole_run_replay_is_unchanged_without_from_step(monkeypatch):
    (status, payload), messages = replay(monkeypatch)

    assert status == 202
    assert "from_step" not in payload
    envelope = json.loads(messages[0]["MessageBody"])
    assert envelope["connector"] == "email"  # the plain trigger event
    assert envelope["id"].startswith("replay-")
    assert envelope["correlation_id"] == "evt-1"
    assert envelope["data"] == {"subject": "invoice", "route": "find"}
    assert "dapier_resume" not in envelope


def test_worker_runs_the_resume_from_the_chosen_step(monkeypatch):
    ran = []
    monkeypatch.setattr(worker, "_run_connector", lambda action, event, wf, steps=None:
                        ran.append((action["id"], json.loads(json.dumps(steps))))
                        or {"sent": True})

    (status, payload), messages = replay(monkeypatch, from_step="post")
    # The worker's ledger replaces the runs-read fake (replay() configured it
    # first; this patch wins).
    _configure_worker(monkeypatch, calls := [])
    resume_message = json.loads(messages[0]["MessageBody"])
    resume_message["dapier_resume"]["resume_at"] = time.time() - 1  # queue latency
    result = worker.handler({"Records": [{"messageId": "m1",
                                          "body": json.dumps(resume_message)}]}, None)

    assert result == {"batchItemFailures": []}
    # Only the chosen step and everything after it re-executed.
    assert [action_id for action_id, _steps in ran] == ["post", "notify"]
    # The follow-up step sees the seeded prior output: {steps.find.output.*}
    # resolves against real recorded data, plus the fresh post output.
    notify_context = ran[-1][1]
    assert notify_context["find"]["output"] == {"rows": 3, "match": "INV-9001"}
    assert notify_context["post"]["status"] == "completed"
    assert notify_context["post"]["output"] == {"sent": True}
    # The earlier step is recorded in the rerun's history as reused, under
    # the rerun's own run id — not re-executed, not missing.
    reused = [kwargs["Item"] for kind, kwargs in calls
              if kind == "put" and kwargs["Item"].get("status") == "reused"]
    assert [item["action_id"] for item in reused] == ["find"]
    assert reused[0]["run_id"] == payload["run_id"]
    assert reused[0]["workflow_id"] == "wf-1"
    assert reused[0]["output"] == {"rows": 3, "match": "INV-9001"}
    # The re-executed steps went through the same processing lease as a
    # fresh run, then closed out completed.
    processing = [kwargs["Item"]["action_id"] for kind, kwargs in calls
                  if kind == "put" and kwargs["Item"].get("status") == "processing"]
    assert processing == ["post", "notify"]


def test_a_delay_resume_envelope_records_no_reused_steps(monkeypatch):
    # Regression guard for the additive envelope key: the delay park path
    # never carries reused_steps and must not grow stray records.
    _configure_worker(monkeypatch, calls := [])
    monkeypatch.setattr(worker, "_run_connector",
                        lambda action, event, wf, steps=None: {"ok": True})
    envelope = {"workflow_id": "wf-1",
                "event": {"id": "evt-1", "connector": "email",
                          "event": "message.received", "data": {}},
                "resume_at": time.time() - 1,
                "delay_action_id": "pause", "paused_ids": ["pause"],
                "segments": [{"steps": [POST_DEF], "prefix": "", "scope": None}],
                "step_outputs": {"pause": {"status": "delayed", "output": {}}},
                "run_id": "wf-1:evt-1"}

    worker._resume_run(envelope, queue=FakeSqs())

    # The chain re-runs post through the normal lease (processing records
    # are expected), but no step ever reads ``reused``: the delay path does
    # not carry reused_steps.
    assert not [kwargs for kind, kwargs in calls
                if kind == "put" and kwargs["Item"].get("status") == "reused"]


def test_a_dropped_envelope_records_no_reused_steps(monkeypatch):
    # Workflow gone: the envelope is consumed and nothing is recorded.
    _configure_worker(monkeypatch, calls := [], all_workflows=[])
    envelope = {"workflow_id": "wf-1",
                "event": {"id": "evt-1", "connector": "email",
                          "event": "message.received", "data": {}},
                "resume_at": time.time() - 1,
                "paused_ids": [], "segments": [], "step_outputs": {},
                "reused_steps": [{"action_id": "find", "output": {}}],
                "run_id": "wf-1:evt-1"}

    assert worker._resume_run(envelope, queue=FakeSqs()) is None
    assert calls == []


def test_run_summary_rolls_reused_steps_up_quietly():
    reused = {"execution_id": "wf-1:find:evt-9", "run_id": "wf-1:evt-9",
              "action_id": "find", "status": "reused",
              "started_at": "2026-09-26T10:00:00+00:00",
              "finished_at": "2026-09-26T10:00:00+00:00"}

    summary = runs.run_summary("wf-1:evt-9", [reused, dict(reused, action_id="post",
                                                           status="completed",
                                                           started_at="2026-09-26T10:00:01+00:00")])
    assert summary["status"] == "completed"
    assert summary["steps"] == 2

    summary = runs.run_summary("wf-1:evt-9", [reused, dict(reused, action_id="post",
                                                           status="failed",
                                                           started_at="2026-09-26T10:00:01+00:00",
                                                           error="again")])
    assert summary["status"] == "failed"
    assert summary["failed_step"] == "post"


# --- surfaces ---------------------------------------------------------------


def _operator_cookies(monkeypatch):
    """An authenticated operator session, the way test_admin builds one."""
    from src.dapier.auth import session

    monkeypatch.setattr(session, "_credentials", lambda: {"username": "admin", "password": "pw"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    return [f"dapier_session={cookie}"]


def _admin_event(method, path, body, cookies):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "origin": "https://dapier.example.test"},
        "cookies": cookies,
        "body": json.dumps(body) if body is not None else None,
    }
    return event


def _wire_queue(monkeypatch):
    monkeypatch.setattr(runs, "_workflows_now", lambda: [WORKFLOW])
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    sqs = FakeSqs()
    monkeypatch.setattr(runs, "_queue", lambda: sqs)
    return sqs


def test_admin_replay_route_passes_from_step(monkeypatch):
    from src.dapier.api import admin

    _configure_runs_read(monkeypatch, history())
    cookies = _operator_cookies(monkeypatch)
    sqs = _wire_queue(monkeypatch)

    replayed = admin.route(
        _admin_event("POST", f"/api/admin/runs/{RUN_ID}/replay",
                     {"from_step": "post"}, cookies),
        "POST", f"/api/admin/runs/{RUN_ID}/replay")

    assert replayed["statusCode"] == 202
    body = json.loads(replayed["body"])
    assert body["from_step"] == "post"
    assert "dapier_resume" in json.loads(sqs.messages[0]["MessageBody"])


def test_admin_replay_route_without_body_keeps_whole_run(monkeypatch):
    from src.dapier.api import admin

    _configure_runs_read(monkeypatch, history())
    cookies = _operator_cookies(monkeypatch)
    sqs = _wire_queue(monkeypatch)

    replayed = admin.route(
        _admin_event("POST", f"/api/admin/runs/{RUN_ID}/replay", None, cookies),
        "POST", f"/api/admin/runs/{RUN_ID}/replay")

    assert replayed["statusCode"] == 202
    envelope = json.loads(sqs.messages[0]["MessageBody"])
    assert "dapier_resume" not in envelope
    assert "from_step" not in json.loads(replayed["body"])


def test_cli_replay_passes_from_step_through(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"accepted": True, "replayed_from": RUN_ID, "from_step": "post",
                "event_id": f"replay-{uuid.uuid4()}", "run_id": "wf-1:replay-abc"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "replay", RUN_ID, "--from-step", "post"])

    assert rc == 0
    assert calls == [("POST", "/api/agent/runs/wf-1%3Aevt-1/replay",
                      {"from_step": "post"})]
    out = capsys.readouterr().out
    assert "post" in out and "wf-1:replay-abc" in out


def test_cli_replay_without_flag_sends_empty_body(isolated_home, monkeypatch):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        return {"accepted": True, "replayed_from": RUN_ID,
                "event_id": "replay-abc", "run_id": "wf-1:replay-abc"}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "replay", RUN_ID])

    assert rc == 0
    assert calls == [("POST", "/api/agent/runs/wf-1%3Aevt-1/replay", {})]


if __name__ == "__main__":
    pytest.main([__file__])
