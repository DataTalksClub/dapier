"""Run history: grouping executions into runs and the per-step flow."""
import json
import time

import boto3
import pytest

from src.dapier.api import runs


class FakeExecTable:
    """Query answers the runs-by-run-id GSI; scan answers the ledger list."""

    def __init__(self, items):
        self.items = items

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def query(self, **kwargs):
        values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
        wanted = values[0] if values else None
        return {"Items": [item for item in self.items if item.get("run_id") == wanted]}


def _configure(monkeypatch, items):
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    class Dynamo:
        def Table(self, _name):
            return FakeExecTable(items)

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


def _step(workflow_id, action_id, event_id, *, status="completed", started="2026-09-25T10:00:00+00:00",
          finished=None, action_type="slack", connector="email", error=None,
          duration=None, input_data=None, output=None, run_id=None):
    item = {
        "execution_id": f"{workflow_id}:{action_id}:{event_id}",
        "workflow_id": workflow_id,
        "action_id": action_id,
        "status": status,
        "started_at": started,
        "connector": connector,
        "event_type": "message.received",
        "correlation_id": event_id,
        "expires_at": 1789000000,
        "duration_ms": duration,
        "input": input_data,
        "output": output,
    }
    if action_type:
        item["action_type"] = action_type
    if error:
        item["error"] = error
    if finished:
        item["finished_at"] = finished
    if run_id:
        item["run_id"] = run_id
    return item


def test_run_id_falls_back_for_legacy_rows():
    item = {"execution_id": "wf-1:post:evt-7"}
    assert runs.run_id_of(item) == "wf-1:evt-7"
    assert runs.run_id_of({"execution_id": "solo", "run_id": "wf:evt"}) == "wf:evt"


def test_recent_groups_steps_by_run_and_orders_newest_first(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", started="2026-09-25T10:00:00+00:00",
              finished="2026-09-25T10:00:01+00:00"),
        _step("wf-1", "upload", "evt-1", started="2026-09-25T10:00:01+00:00",
              finished="2026-09-25T10:00:03+00:00", action_type="dropbox_upload"),
        _step("wf-1", "post", "evt-2", started="2026-09-25T11:00:00+00:00", status="processing"),
    ])

    status, payload = runs.api_list()
    assert status == 200
    runs_list = payload["runs"]
    assert [run["run_id"] for run in runs_list] == ["wf-1:evt-2", "wf-1:evt-1"]
    grouped = runs_list[1]
    assert grouped["steps"] == 2
    assert grouped["status"] == "completed"
    assert grouped["workflow_id"] == "wf-1"
    assert grouped["started_at"] == "2026-09-25T10:00:00+00:00"
    assert grouped["finished_at"] == "2026-09-25T10:00:03+00:00"
    assert grouped["duration_ms"] is None or isinstance(grouped["duration_ms"], int)


def test_run_summary_failed_wins_over_processing(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", status="failed", error="boom",
              run_id="wf-1:evt-1", started="2026-09-25T10:00:00+00:00"),
        _step("wf-1", "post2", "evt-1", status="processing", run_id="wf-1:evt-1",
              started="2026-09-25T10:00:02+00:00"),
    ])

    status, payload = runs.api_list()
    run = payload["runs"][0]
    assert run["status"] == "failed"
    assert run["error"] == "boom"
    assert run["failed_step"] == "post"


def test_run_summary_filtered_is_finished_not_processing(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "gate", "evt-1", status="filtered", action_type="filter",
              run_id="wf-1:evt-1", started="2026-09-25T10:00:00+00:00",
              finished="2026-09-25T10:00:01+00:00"),
        _step("wf-1", "post", "evt-1", status="completed",
              run_id="wf-1:evt-1", started="2026-09-25T10:00:01+00:00",
              finished="2026-09-25T10:00:02+00:00"),
    ])

    status, payload = runs.api_list()
    run = payload["runs"][0]
    assert run["status"] == "filtered"
    assert run["failed_step"] is None
    assert run["error"] is None


def test_api_get_returns_steps_in_execution_order(monkeypatch):
    steps = [
        _step("wf-1", "post", "evt-1", started="2026-09-25T10:00:00+00:00",
              finished="2026-09-25T10:00:01+00:00", duration=980,
              input_data={"subject": "invoice"}, output={"status": 200},
              run_id="wf-1:evt-1"),
        _step("wf-1", "upload", "evt-1", started="2026-09-25T10:00:01+00:00",
              finished="2026-09-25T10:00:03+00:00", action_type="dropbox_upload",
              duration=2000, input_data={"subject": "invoice"},
              output={"uploaded": ["/Invoices/x.pdf"]},
              run_id="wf-1:evt-1"),
    ]
    _configure(monkeypatch, list(reversed(steps)))

    status, payload = runs.api_get("wf-1:evt-1")
    assert status == 200
    assert payload["run"]["status"] == "completed"
    assert payload["run"]["duration_ms"] == 980 + 2000
    assert [step["action_id"] for step in payload["steps"]] == ["post", "upload"]
    assert payload["steps"][0]["output"] == {"status": 200}
    assert payload["steps"][0]["action_type"] == "slack"


def test_api_get_enriches_legacy_rows_without_run_id(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", started="2026-09-25T10:00:00+00:00",
              finished="2026-09-25T10:00:01+00:00"),
    ])

    status, payload = runs.api_get("wf-1:evt-1")
    assert status == 200
    assert payload["run"]["run_id"] == "wf-1:evt-1"
    assert payload["steps"][0]["execution_id"] == "wf-1:post:evt-1"


def test_api_get_unknown_run_is_404_and_blank_is_400(monkeypatch):
    _configure(monkeypatch, [])
    assert runs.api_get("wf-1:nope")[0] == 404
    assert runs.api_get("  ")[0] == 400


def test_decimal_numbers_are_json_safe(monkeypatch):
    from decimal import Decimal

    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", duration=Decimal("150"), run_id="wf-1:evt-1",
              started="2026-09-25T10:00:00+00:00", finished="2026-09-25T10:00:01+00:00"),
    ])

    status, payload = runs.api_get("wf-1:evt-1")
    assert json.dumps(payload)  # no Decimal escapes into the response body
    assert payload["steps"][0]["duration_ms"] == 150
    assert payload["steps"][0]["expires_at"] == 1789000000


class FakeQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return {"MessageId": "sqsm-1"}


def _configure_queue(monkeypatch):
    monkeypatch.setenv("EVENT_QUEUE_URL", "https://sqs.test/events")
    queue = FakeQueue()
    monkeypatch.setattr(runs, "_queue", lambda: queue)
    return queue


def test_api_replay_reinjects_the_original_event(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", run_id="wf-1:evt-1", connector="email",
              input_data={"route": "invoice", "subject": "hello"}),
    ])
    queue = _configure_queue(monkeypatch)

    status, payload = runs.api_replay("wf-1:evt-1")

    assert status == 202
    assert payload["accepted"] is True
    assert payload["replayed_from"] == "wf-1:evt-1"
    assert payload["run_id"].startswith("wf-1:replay-")
    event = json.loads(queue.messages[0]["MessageBody"])
    assert queue.messages[0]["QueueUrl"] == "https://sqs.test/events"
    assert event["id"].startswith("replay-")  # a fresh run in history
    assert event["correlation_id"] == "evt-1"  # tied to the original event
    assert event["connector"] == "email"
    assert event["event"] == "message.received"
    assert event["data"] == {"route": "invoice", "subject": "hello"}


def test_api_replay_blank_is_400_and_unknown_is_404(monkeypatch):
    _configure(monkeypatch, [])
    _configure_queue(monkeypatch)
    assert runs.api_replay("  ")[0] == 400
    assert runs.api_replay("wf-1:nope")[0] == 404


def test_api_replay_refuses_runs_without_recorded_event_data(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", run_id="wf-1:evt-1", input_data=None),
    ])
    _configure_queue(monkeypatch)

    status, payload = runs.api_replay("wf-1:evt-1")

    assert status == 409
    assert "cannot be replayed" in payload["error"]


def test_api_replay_refuses_truncated_event_data(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", run_id="wf-1:evt-1",
              input_data={"truncated": True, "preview": '{"route": "inv'}),
    ])
    _configure_queue(monkeypatch)

    status, payload = runs.api_replay("wf-1:evt-1")

    assert status == 409
    assert "too large" in payload["error"]


def test_api_replay_reinjects_a_20kb_webhook_event(monkeypatch):
    """A 20 KB hook body runs fine at intake, so it must replay too: the
    trigger input is captured at the raised replay cap, not the step-output
    cap that used to truncate it into a permanent refusal."""
    big = {"body": "x" * 20_000}
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", run_id="wf-1:evt-1", input_data=big),
    ])
    queue = _configure_queue(monkeypatch)

    status, payload = runs.api_replay("wf-1:evt-1")

    assert status == 202
    assert json.loads(queue.messages[0]["MessageBody"])["data"] == big


def test_recent_hides_failure_notice_items(monkeypatch):
    notice = _step("wf-1", "failure-notice", "evt-1", status="notified",
                   run_id="wf-1:evt-1#notice", started="2026-09-25T10:00:05+00:00")
    notice["kind"] = "failure-notice"
    notice["execution_id"] = "wf-1:failure-notice:evt-1"
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", run_id="wf-1:evt-1",
              started="2026-09-25T10:00:00+00:00", finished="2026-09-25T10:00:01+00:00"),
        notice,
    ])

    status, payload = runs.api_list()

    assert status == 200
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]
    assert payload["runs"][0]["steps"] == 1


def test_replay_event_tolerates_a_colon_free_run_id():
    event, error = runs.replay_event("legacy-run", [
        {"connector": "webhook", "event_type": "received", "input": {"ok": True}},
    ])
    assert error is None
    assert event["correlation_id"] == "legacy-run"


def test_api_list_filters_by_workflow(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1"),
        _step("wf-2", "post", "evt-2"),
        _step("wf-1", "post", "evt-3"),
    ])

    status, payload = runs.api_list(workflow_id="wf-1")
    assert status == 200
    listed = payload["runs"]
    assert len(listed) == 2
    assert all(run["workflow_id"] == "wf-1" for run in listed)


def test_api_list_filters_by_status(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", status="failed"),
        _step("wf-2", "post", "evt-2"),
    ])

    _, payload = runs.api_list(status="failed")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]


def test_api_list_filters_by_since(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", started="2026-09-25T10:00:00+00:00"),
        _step("wf-2", "post", "evt-2", started="2026-09-26T10:00:00+00:00"),
        _step("wf-1", "post", "evt-3", started="2026-09-27T10:00:00+00:00"),
    ])

    _, payload = runs.api_list(since="2026-09-26T00:00:00+00:00")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-3", "wf-2:evt-2"]


def test_api_list_combines_filters(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", started="2026-09-25T10:00:00+00:00"),
        _step("wf-1", "post", "evt-2", started="2026-09-26T10:00:00+00:00", status="failed"),
        _step("wf-2", "post", "evt-3", started="2026-09-27T10:00:00+00:00"),
    ])

    _, payload = runs.api_list(workflow_id="wf-1", status="failed",
                               since="2026-09-26T00:00:00+00:00")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-2"]


def test_api_list_content_search_matches_recorded_data(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", started="2026-09-25T10:00:00+00:00",
              input_data={"text": "standup notes"},
              output={"thread_ts": "1727260000.1", "permalink": "https://x.invalid/inv-42"}),
        _step("wf-1", "post", "evt-2", started="2026-09-26T10:00:00+00:00",
              input_data={"text": "lunch order"}),
    ])

    status, payload = runs.api_list(q="INV-42")
    assert status == 200
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]
    assert payload["paging"]["filtered"] is True

    status, payload = runs.api_list(q="no-such-needle")
    assert status == 200
    assert payload["runs"] == []


def test_api_list_content_search_matches_run_ids_and_ignores_blank(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1"),
        _step("wf-2", "post", "evt-2"),
    ])

    _, payload = runs.api_list(q="wf-2:evt-2")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-2:evt-2"]

    _, payload = runs.api_list(q="   ")
    assert len(payload["runs"]) == 2
    assert payload["paging"]["filtered"] is False


def test_api_replay_from_step_builds_the_resume_envelope(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "fetch", "evt-1", started="2026-09-25T10:00:00+00:00",
              finished="2026-09-25T10:00:01+00:00", run_id="wf-1:evt-1", connector="email",
              input_data={"route": "invoice"}, output={"rows": 3}),
        _step("wf-1", "post", "evt-1", started="2026-09-25T10:00:01+00:00",
              finished="2026-09-25T10:00:02+00:00", run_id="wf-1:evt-1", status="failed",
              error="boom", action_type="webhook"),
    ])
    queue = _configure_queue(monkeypatch)
    monkeypatch.setattr(runs, "_workflows_now", lambda: [{
        "id": "wf-1", "enabled": True,
        "actions": [{"id": "fetch", "type": "webhook"},
                    {"id": "post", "type": "webhook"},
                    {"id": "notify", "type": "slack"}],
    }])

    status, payload = runs.api_replay("wf-1:evt-1", from_step="post")

    assert status == 202
    assert payload["accepted"] is True
    assert payload["from_step"] == "post"
    assert payload["replayed_from"] == "wf-1:evt-1"
    assert payload["run_id"].startswith("wf-1:replay-")
    resume = json.loads(queue.messages[0]["MessageBody"])["dapier_resume"]
    assert resume["workflow_id"] == "wf-1"
    assert resume["event"]["id"].startswith("replay-")  # a fresh run in history
    assert resume["event"]["correlation_id"] == "evt-1"  # tied to the original
    assert resume["event"]["data"] == {"route": "invoice"}
    assert [step["id"] for step in resume["segments"][0]["steps"]] == ["post", "notify"]
    # everything recorded before the chosen step seeds the rerun; the chosen
    # step and beyond re-execute
    assert resume["step_outputs"] == {"fetch": {"status": "completed", "output": {"rows": 3}}}
    assert resume["paused_ids"] == []
    assert resume["resume_at"] <= time.time()  # the worker runs it on arrival


def test_api_replay_from_step_never_reached_seeds_everything(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "fetch", "evt-1", started="2026-09-25T10:00:00+00:00",
              finished="2026-09-25T10:00:01+00:00", run_id="wf-1:evt-1", connector="email",
              input_data={"route": "invoice"}, output={"rows": 3}),
    ])
    queue = _configure_queue(monkeypatch)
    monkeypatch.setattr(runs, "_workflows_now", lambda: [{
        "id": "wf-1", "enabled": True,
        "actions": [{"id": "fetch", "type": "webhook"},
                    {"id": "notify", "type": "slack"}],
    }])

    status, payload = runs.api_replay("wf-1:evt-1", from_step="notify")

    assert status == 202
    resume = json.loads(queue.messages[0]["MessageBody"])["dapier_resume"]
    assert [step["id"] for step in resume["segments"][0]["steps"]] == ["notify"]
    assert set(resume["step_outputs"]) == {"fetch"}


def test_api_replay_from_step_refuses_gone_disabled_and_foreign_steps(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", run_id="wf-1:evt-1", connector="email",
              input_data={"route": "invoice"}),
    ])
    _configure_queue(monkeypatch)
    actions = [{"id": "post", "type": "webhook"}]

    monkeypatch.setattr(runs, "_workflows_now", lambda: [])
    status, payload = runs.api_replay("wf-1:evt-1", from_step="post")
    assert status == 404
    assert "no longer exists" in payload["error"]

    monkeypatch.setattr(runs, "_workflows_now",
                        lambda: [{"id": "wf-1", "enabled": False, "actions": actions}])
    status, payload = runs.api_replay("wf-1:evt-1", from_step="post")
    assert status == 409
    assert "disabled" in payload["error"]

    monkeypatch.setattr(runs, "_workflows_now",
                        lambda: [{"id": "wf-1", "enabled": True,
                                  "actions": [{"id": "other", "type": "webhook"}]}])
    status, payload = runs.api_replay("wf-1:evt-1", from_step="post")
    assert status == 409
    assert "not a top-level step" in payload["error"]


# --- CSV export ---------------------------------------------------------------


def test_api_export_returns_the_newest_runs_as_csv(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-1"),
        _step("wf-b", "post", "evt-2", status="failed", error="Slack said no",
              started="2026-09-25T11:00:00+00:00", finished="2026-09-25T11:00:02+00:00",
              duration=1200),
    ])

    status, payload = runs.api_export(now=1758868800)

    assert status == 200
    assert payload["count"] == 2
    assert payload["truncated"] is False
    assert payload["filename"].startswith("dapier-runs-")
    lines = payload["csv"].splitlines()
    assert lines[0] == ",".join(runs.CSV_COLUMNS)
    # Newest first; booleans read true/false, missing fields read "". The
    # failure has no resolution: nothing fixed it and no later run of wf-b
    # completed, so resolved and resolved_reason are both empty.
    assert lines[1] == ("wf-b:evt-2,wf-b,email,message.received,failed,false,,false,"
                        "1,,post,,"
                        "2026-09-25T11:00:00+00:00,2026-09-25T11:00:02+00:00,1200,"
                        "Slack said no")


def test_api_export_applies_the_list_filters_and_flags_truncation(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-a", "post", "evt-1"),
        _step("wf-b", "post", "evt-2"),
        _step("wf-c", "post", "evt-3", input_data={"subject": "invoice-7"}),
    ])

    _, payload = runs.api_export(workflow_id="wf-b")
    assert payload["count"] == 1
    assert "wf-b:evt-2" in payload["csv"]

    _, payload = runs.api_export(status="success")
    assert payload["count"] == 3

    # Content search matches what the list's q matches: recorded step data.
    _, payload = runs.api_export(q="invoice-7")
    assert payload["count"] == 1
    assert "wf-c:evt-3" in payload["csv"]

    _, payload = runs.api_export(max_rows=2)
    assert payload["count"] == 2
    assert payload["truncated"] is True


def test_api_export_clamps_max_rows(monkeypatch):
    _configure(monkeypatch, [_step("wf-a", "post", "evt-1")])

    _, payload = runs.api_export(max_rows="not-a-number")
    assert payload["count"] == 1

    _, payload = runs.api_export(max_rows=10 ** 9)
    assert payload["count"] == 1
    assert payload["truncated"] is False


if __name__ == "__main__":
    pytest.main([__file__])
