"""Failure resolution: the derived recovery a completed rerun gives an old
failure, the operator's explicit mark, and how both leave the failure views.

The story: a run fails, a replay or a fix gets it working, and the red row
stops being work. Recovery is derived (nothing writes it), so the tests read
history and assert the verdict; the explicit mark is a write and asserts the
stamp that landed.
"""
import json
from datetime import datetime, timezone

import boto3
import pytest

from src.dapier.api import errors, runs


class FakeExecTable:
    """Query answers the runs-by-run-id GSI; scan answers the ledger list.

    ``updates`` records every ``update_item`` call so a test can assert what
    the resolve mark actually wrote — the write is the whole contract of the
    operator button.
    """

    def __init__(self, items):
        self.items = items
        self.updates = []

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def query(self, **kwargs):
        values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
        wanted = values[0] if values else None
        return {"Items": [item for item in self.items if item.get("run_id") == wanted]}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        key = (kwargs.get("Key") or {}).get("execution_id")
        values = kwargs.get("ExpressionAttributeValues") or {}
        for item in self.items:
            if item.get("execution_id") == key:
                item["resolved_at"] = values.get(":at")
                item["resolved_reason"] = values.get(":reason")
                item["resolved_by"] = values.get(":by")
                break
        return {}


def _configure(monkeypatch, items):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    table = FakeExecTable(items)

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return table


def _step(workflow_id, action_id, event_id, *, status="completed",
          started="2026-09-25T10:00:00+00:00", error=None, run_id=None,
          **extra):
    item = {
        "execution_id": f"{workflow_id}:{action_id}:{event_id}",
        "run_id": run_id or f"{workflow_id}:{event_id}",
        "workflow_id": workflow_id,
        "action_id": action_id,
        "action_type": "slack",
        "connector": "email",
        "event_type": "message.received",
        "correlation_id": event_id,
        "status": status,
        "started_at": started,
        "error": error,
        "input": {"subject": "invoice"},
    }
    item.update(extra)
    return item


def _failed_run(*, workflow_id="wf-a", event_id="evt-1",
                started="2026-09-25T10:00:00+00:00", steps=1, **extra):
    return [_step(workflow_id, f"step-{index}", event_id, status="failed",
                  started=started, error="Slack said no", **extra)
            for index in range(steps)]


def _run_ids(payload):
    return [run["run_id"] for run in payload["runs"]]


# --- derived recovery: what a successful replay gives the failure it fixed


def test_a_later_completed_run_recovers_the_earlier_failure(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-2", started="2026-09-26T10:00:00+00:00"),
        *_failed_run(),
    ])

    status, payload = runs.api_list(status="failed")

    assert status == 200
    assert _run_ids(payload) == ["wf-a:evt-1"]
    recovered = payload["runs"][0]
    assert recovered["resolved"] is True
    assert recovered["resolved_reason"] == "recovered"
    assert recovered["resolved_at"] == "2026-09-26T10:00:00+00:00"
    # The verdict rides on the failure; the run's own outcome never changes.
    assert recovered["status"] == "failed"
    assert recovered["error"] == "Slack said no"


def test_the_replay_lands_as_a_new_run_and_settles_the_old_failure(monkeypatch):
    """The end-to-end shape of "replay it and it works": the replay mints a
    fresh event id, its run completes, and the failure it was meant to fix is
    no longer a problem — with nothing written to say so."""
    table = _configure(monkeypatch, _failed_run())

    status, payload = runs.api_list(status="problems")
    assert _run_ids(payload) == ["wf-a:evt-1"]
    assert payload["runs"][0]["resolved"] is False

    # The replay: a new run of the same workflow, a new event, completed.
    table.items.append(_step("wf-a", "post", "replay-abc",
                             started="2026-09-26T10:00:00+00:00"))

    status, payload = runs.api_list(status="problems")
    assert payload["runs"] == []  # the failure is out of the actionable set
    assert not [call for call in table.updates if call.get("UpdateExpression")]
    assert _run_ids(runs.api_list(status="resolved")[1]) == ["wf-a:evt-1"]


def test_the_exact_failed_status_still_shows_the_recovered_run(monkeypatch):
    """``failed`` is the raw status, so it keeps the failure a completed
    rerun settled — the record is not rewritten. ``problems`` is the
    actionable set and drops it; nothing filters, both are in history."""
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-2", started="2026-09-26T10:00:00+00:00"),
        *_failed_run(),
    ])

    assert _run_ids(runs.api_list(status="failed")[1]) == ["wf-a:evt-1"]
    assert _run_ids(runs.api_list(status="problems")[1]) == []
    assert _run_ids(runs.api_list(status="resolved")[1]) == ["wf-a:evt-1"]
    assert _run_ids(runs.api_list()[1]) == ["wf-a:evt-2", "wf-a:evt-1"]


def test_a_failure_with_no_later_completed_run_stays_a_problem(monkeypatch):
    _configure(monkeypatch, _failed_run())

    status, payload = runs.api_list(status="problems")

    assert _run_ids(payload) == ["wf-a:evt-1"]
    assert payload["runs"][0]["resolved"] is False
    assert payload["runs"][0]["resolved_at"] is None


def test_a_filtered_rerun_does_not_recover_the_failure(monkeypatch):
    """A ``filtered`` run stopped before the step that failed — it proves the
    trigger still matches and nothing more, so the failure keeps needing
    action."""
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-2", status="filtered",
              started="2026-09-26T10:00:00+00:00"),
        *_failed_run(),
    ])

    status, payload = runs.api_list(status="problems")

    assert _run_ids(payload) == ["wf-a:evt-1"]
    assert payload["runs"][0]["resolved"] is False


def test_an_unfinished_rerun_does_not_recover_the_failure(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-2", status="processing",
              started="2026-09-26T10:00:00+00:00"),
        *_failed_run(),
    ])

    status, payload = runs.api_list(status="problems")

    assert _run_ids(payload) == ["wf-a:evt-1"]
    assert payload["runs"][0]["resolved"] is False


def test_a_completed_run_before_the_failure_recovers_nothing(monkeypatch):
    """Order matters: a success that predates the failure says nothing about
    it — the break came after."""
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-0", started="2026-09-24T10:00:00+00:00"),
        *_failed_run(),
    ])

    status, payload = runs.api_list(status="problems")

    assert _run_ids(payload) == ["wf-a:evt-1"]
    assert payload["runs"][0]["resolved"] is False


def test_recovery_does_not_cross_workflows(monkeypatch):
    """wf-b's healthy runs say nothing about wf-a's failure."""
    _configure(monkeypatch, [
        _step("wf-b", "post", "evt-9", started="2026-09-26T10:00:00+00:00"),
        *_failed_run(),
    ])

    status, payload = runs.api_list(status="problems")

    assert _run_ids(payload) == ["wf-a:evt-1"]
    assert payload["runs"][0]["resolved"] is False


def test_a_second_failure_stays_unresolved_while_the_first_recovers(monkeypatch):
    """Recovery retires the failures a fix actually settled, not the
    workflow's current breakage: the newest failure is the open work."""
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-fix", started="2026-09-26T10:00:00+00:00"),
        _step("wf-a", "post", "evt-3", status="failed",
              started="2026-09-27T10:00:00+00:00", error="Slack said no again"),
        *_failed_run(),
    ])

    status, payload = runs.api_list(status="problems")

    assert _run_ids(payload) == ["wf-a:evt-3"]
    assert payload["runs"][0]["resolved"] is False
    assert _run_ids(runs.api_list(status="resolved")[1]) == ["wf-a:evt-1"]


# --- the two halves of the failure set


def test_the_failure_set_splits_into_problems_and_resolved(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-fix", started="2026-09-26T10:00:00+00:00"),
        _step("wf-a", "post", "evt-ok", started="2026-09-25T12:00:00+00:00"),
        *_failed_run(event_id="evt-open",
                     started="2026-09-27T10:00:00+00:00"),
        *_failed_run(event_id="evt-old",
                     started="2026-09-25T10:00:00+00:00"),
    ])

    assert _run_ids(runs.api_list(status="problems")[1]) == ["wf-a:evt-open"]
    assert _run_ids(runs.api_list(status="resolved")[1]) == ["wf-a:evt-old"]
    # A healthy run is in neither set: it has nothing to resolve.
    assert "wf-a:evt-ok" not in _run_ids(runs.api_list(status="resolved")[1])
    # The completing runs themselves are healthy: no verdict to carry.
    assert _run_ids(runs.api_list(status="success")[1]) == [
        "wf-a:evt-fix", "wf-a:evt-ok"]
    assert all(row["resolved"] is False
               for row in runs.api_list(status="success")[1]["runs"])


def test_the_resolved_view_composes_with_the_other_filters(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-fix", started="2026-09-26T10:00:00+00:00"),
        _step("wf-b", "post", "evt-8", started="2026-09-27T10:00:00+00:00"),
        *_failed_run(workflow_id="wf-a"),
    ])

    status, payload = runs.api_list(workflow_id="wf-a", status="resolved")

    assert status == 200
    assert _run_ids(payload) == ["wf-a:evt-1"]
    assert payload["paging"]["filtered"] is True


def test_the_page_token_still_works_across_the_failure_views(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-fix", started="2026-09-28T10:00:00+00:00"),
        *_failed_run(event_id="evt-2", started="2026-09-26T10:00:00+00:00"),
        *_failed_run(event_id="evt-1"),
    ])

    status, first = runs.api_list(limit=1, status="resolved")

    assert status == 200
    assert _run_ids(first) == ["wf-a:evt-2"]
    status, second = runs.api_list(limit=1, status="resolved",
                                   next_token=first["paging"]["next"])
    assert _run_ids(second) == ["wf-a:evt-1"]


# --- the operator's mark


def test_api_resolve_stamps_every_step_of_the_run(monkeypatch):
    table = _configure(monkeypatch, _failed_run(steps=3))

    status, payload = runs.api_resolve("wf-a:evt-1", by="op-sub",
                                       note="superseded by the new address")

    assert status == 200
    assert payload["resolved"] is True
    assert payload["already_resolved"] is False
    assert payload["steps"] == 3
    assert payload["note"] == "superseded by the new address"
    assert len(table.updates) == 3
    for call in table.updates:
        values = call["ExpressionAttributeValues"]
        assert values[":reason"] == "acknowledged"
        assert values[":by"] == "op-sub"
        assert values[":at"]
    run = payload["run"]
    assert run["resolved"] is True
    assert run["resolved_reason"] == "acknowledged"
    assert run["resolved_by"] == "op-sub"
    # The run keeps its failure — a fixed run is still a failed run.
    assert run["status"] == "failed"
    assert run["error"] == "Slack said no"


def test_a_marked_failure_leaves_the_problem_views(monkeypatch):
    _configure(monkeypatch, _failed_run())

    runs.api_resolve("wf-a:evt-1", by="op-sub")

    assert runs.api_list(status="problems")[1]["runs"] == []
    assert _run_ids(runs.api_list(status="resolved")[1]) == ["wf-a:evt-1"]
    assert _run_ids(runs.api_list()[1]) == ["wf-a:evt-1"]  # still in history


def test_resolving_twice_is_idempotent(monkeypatch):
    table = _configure(monkeypatch, _failed_run())

    first_status, first = runs.api_resolve("wf-a:evt-1", by="op-sub")
    second_status, second = runs.api_resolve("wf-a:evt-1", by="other-sub")

    assert first_status == second_status == 200
    assert first["already_resolved"] is False
    assert second["already_resolved"] is True
    assert second["resolved_reason"] == "acknowledged"
    assert second["resolved_at"] == first["resolved_at"]
    assert second["run"]["resolved_by"] == "op-sub"  # the first mark stands
    assert len(table.updates) == 1


def test_the_operator_mark_survives_a_later_failure(monkeypatch):
    """The stamp is on the run, not the workflow: a fresh failure of the same
    workflow is a new run and starts unresolved."""
    table = _configure(monkeypatch, _failed_run())
    runs.api_resolve("wf-a:evt-1", by="op-sub")
    table.items.append(_step("wf-a", "post", "evt-2", status="failed",
                             started="2026-09-26T10:00:00+00:00",
                             error="Slack said no again"))

    status, payload = runs.api_list(status="problems")

    assert _run_ids(payload) == ["wf-a:evt-2"]
    assert payload["runs"][0]["resolved"] is False
    assert _run_ids(runs.api_list(status="resolved")[1]) == ["wf-a:evt-1"]


def test_a_healthy_run_cannot_be_marked_fixed(monkeypatch):
    table = _configure(monkeypatch, [_step("wf-a", "post", "evt-1")])

    status, payload = runs.api_resolve("wf-a:evt-1", by="op-sub")

    assert status == 409
    assert "only a failed run" in payload["error"]
    assert table.updates == []


def test_resolving_an_unknown_run_is_404(monkeypatch):
    _configure(monkeypatch, _failed_run())

    status, payload = runs.api_resolve("wf-a:missing", by="op-sub")

    assert status == 404
    assert payload["error"] == "Run not found"


def test_api_resolve_needs_a_run_id(monkeypatch):
    _configure(monkeypatch, [])

    assert runs.api_resolve("  ")[0] == 400


def test_api_get_derives_recovery_for_the_one_run(monkeypatch):
    """The detail view and the list must agree, or the dialog would offer a
    Mark fixed button on a failure the list already calls settled."""
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-2", started="2026-09-26T10:00:00+00:00"),
        *_failed_run(),
    ])

    status, payload = runs.api_get("wf-a:evt-1")

    assert status == 200
    assert payload["run"]["resolved"] is True
    assert payload["run"]["resolved_reason"] == "recovered"


def test_api_get_leaves_an_unrecoverable_failure_unresolved(monkeypatch):
    _configure(monkeypatch, _failed_run())

    status, payload = runs.api_get("wf-a:evt-1")

    assert status == 200
    assert payload["run"]["resolved"] is False


def test_a_resolved_run_is_still_replayable(monkeypatch):
    """Resolving retires a failure from the to-do list; it does not close the
    run to investigation — an operator who changes their mind about the
    verdict still has the replay button and the recorded event."""
    _configure(monkeypatch, _failed_run())
    runs.api_resolve("wf-a:evt-1", by="op-sub")

    status, payload = runs.api_get("wf-a:evt-1")

    assert status == 200
    envelope, error = runs.replay_event("wf-a:evt-1", payload["steps"])
    assert error is None
    assert envelope["id"].startswith("replay-")
    assert envelope["data"] == {"subject": "invoice"}


# --- replay-failed retries the work, not the history


def test_replay_failed_leaves_resolved_failures_alone(monkeypatch):
    """A fixed failure is not what "replay failed" is for — replaying it would
    re-run a workflow the operator already called handled."""
    class Queue:
        def __init__(self):
            self.messages = []

        def send_message(self, **kwargs):
            self.messages.append(json.loads(kwargs["MessageBody"]))

    queue = Queue()
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-fix", started="2026-09-26T10:00:00+00:00"),
        *_failed_run(),
    ])

    status, payload = runs.api_replay_failed("wf-a", queue=queue)

    assert status == 202
    assert payload["replayed"] == 0
    assert queue.messages == []


def test_replay_failed_still_replays_unresolved_failures(monkeypatch):
    class Queue:
        def __init__(self):
            self.messages = []

        def send_message(self, **kwargs):
            self.messages.append(json.loads(kwargs["MessageBody"]))

    queue = Queue()
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://queue.test")
    _configure(monkeypatch, _failed_run())

    status, payload = runs.api_replay_failed("wf-a", queue=queue)

    assert status == 202
    assert payload["replayed"] == 1
    assert payload["runs"][0]["run_id"] == "wf-a:evt-1"
    assert queue.messages[0]["id"].startswith("replay-")


# --- the aggregate views read the same signal


def test_the_error_summary_counts_only_failures_that_need_action(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-fixed", "post", "evt-fix", started="2026-09-26T10:00:00+00:00"),
        _step("wf-open", "post", "evt-old", started="2026-09-24T10:00:00+00:00"),
        *_failed_run(workflow_id="wf-fixed"),
        *_failed_run(workflow_id="wf-open"),
    ])

    status, payload = errors.api_summary(
        now=datetime(2026, 9, 27, tzinfo=timezone.utc))

    assert status == 200
    counted = {row["workflow_id"]: row["failed_runs"]
               for row in payload["workflows"]}
    assert counted == {"wf-open": 1}  # wf-fixed's failure recovered
    assert payload["total_failed_runs"] == 1


def test_the_export_carries_the_resolution_columns(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-2", started="2026-09-26T10:00:00+00:00"),
        *_failed_run(),
    ])

    status, payload = runs.api_export(now=1758868800, status="resolved")

    assert status == 200
    header, row = payload["csv"].splitlines()[:2]
    assert header == ",".join(runs.CSV_COLUMNS)
    assert row.startswith("wf-a:evt-1,wf-a,email,message.received,failed,true,recovered")