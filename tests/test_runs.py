"""Run history: grouping executions into runs and the per-step flow."""
import json

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


if __name__ == "__main__":
    pytest.main([__file__])
