"""Error visibility: failed-run counts grouped by workflow over a window."""
from datetime import datetime, timezone

from src.dapier.api import errors as errors_api


def _run(workflow_id, started, error="boom"):
    return {"run_id": f"{workflow_id}:{started}", "workflow_id": workflow_id,
            "status": "failed", "started_at": started, "error": error}


def _patch_recent(monkeypatch, failed):
    import src.dapier.api.runs as runs
    seen = {}

    def fake_recent(limit, workflow_id=None, status=None, since=None, before=None):
        seen.update(limit=limit, status=status, since=since)
        return failed

    monkeypatch.setattr(runs, "recent", fake_recent)
    return seen


def test_summary_counts_by_workflow_and_keeps_the_newest_failure(monkeypatch):
    failed = [
        _run("wf-1", "2026-09-25T10:00:00+00:00", "Slack rejected message"),
        _run("wf-2", "2026-09-26T09:00:00+00:00"),
        _run("wf-1", "2026-09-26T12:00:00+00:00", "step timeout"),
    ]
    seen = _patch_recent(monkeypatch, failed)

    status, payload = errors_api.api_summary(
        days=7, now=datetime(2026, 9, 27, tzinfo=timezone.utc))

    assert status == 200
    assert seen["status"] == "problems"
    assert seen["since"] == "2026-09-20T00:00:00+00:00"
    rows = {row["workflow_id"]: row for row in payload["workflows"]}
    assert rows["wf-1"]["failed_runs"] == 2
    assert rows["wf-1"]["last_error"] == "step timeout"
    assert rows["wf-1"]["last_failed_at"] == "2026-09-26T12:00:00+00:00"
    assert payload["total_failed_runs"] == 3
    # Most failures first, workflow_id breaking ties.
    assert [row["workflow_id"] for row in payload["workflows"]] == ["wf-1", "wf-2"]


def test_summary_clamps_the_window_and_buckets_unknown_workflows(monkeypatch):
    failed = [{"run_id": "x:evt", "workflow_id": None, "status": "failed",
               "started_at": "2026-09-26T09:00:00+00:00", "error": "boom"}]
    _patch_recent(monkeypatch, failed)

    _, payload = errors_api.api_summary(
        days=500, now=datetime(2026, 9, 27, tzinfo=timezone.utc))

    assert payload["window_days"] == errors_api.MAX_DAYS
    assert [row["workflow_id"] for row in payload["workflows"]] == ["unknown"]
    assert payload["total_failed_runs"] == 1


def test_summary_of_a_quiet_window(monkeypatch):
    _patch_recent(monkeypatch, [])

    status, payload = errors_api.api_summary(
        days=7, now=datetime(2026, 9, 27, tzinfo=timezone.utc))

    assert status == 200
    assert payload["workflows"] == []
    assert payload["total_failed_runs"] == 0


if __name__ == "__main__":
    import pytest

    pytest.main([__file__])
