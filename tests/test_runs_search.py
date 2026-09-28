"""Run history content search (``?q=`` / ``dapier runs list --search``).

Free text matches case-insensitively against a run's recorded step
input/output JSON inside the existing bounded scan window — no new index,
the audit trail's ``q`` approach. Covers the domain filters, their
composition with the workflow/status filters, the blank-query no-op, and
the CLI flag passing through to the agent API.
"""
import json

import boto3
import pytest

from dapier_cli import commands, main
from src.dapier.api import runs


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


class FakeExecTable:
    """Scan answers the list window; query answers the runs-by-run-id GSI."""

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


def _step(workflow_id, action_id, event_id, *, status="completed",
          started="2026-09-25T10:00:00+00:00", input_data=None, output=None,
          error=None):
    return {
        "execution_id": f"{workflow_id}:{action_id}:{event_id}",
        "run_id": f"{workflow_id}:{event_id}",
        "workflow_id": workflow_id,
        "action_id": action_id,
        "action_type": "slack",
        "connector": "email",
        "event_type": "message.received",
        "status": status,
        "started_at": started,
        "input": input_data,
        "output": output,
        "error": error,
    }


def test_search_matches_step_output_content(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "find", "evt-1",
              output={"rows": [{"order": "ORD-77"}], "count": 1}),
        _step("wf-2", "post", "evt-2",
              output={"permalink": "https://x.invalid/other"}),
    ])

    status, payload = runs.api_list(q="ord-77")  # case-insensitive

    assert status == 200
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]
    assert payload["paging"]["filtered"] is True


def test_search_matches_step_input_content(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", input_data={"text": "lunch order"}),
        _step("wf-1", "post", "evt-2", input_data={"text": "standup notes"}),
    ])

    _, payload = runs.api_list(q="standup")

    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-2"]


def test_search_matches_truncated_output_preview(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1",
              output={"truncated": True, "preview": '{"body": "invoice 9001"}'}),
    ])

    _, payload = runs.api_list(q="9001")

    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]


def test_search_composes_with_workflow_and_status_filters(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "find", "evt-1", input_data={"text": "order #1234"},
              status="failed", error="Slack rejected message"),
        _step("wf-1", "find", "evt-2", input_data={"text": "order #1234"}),
        _step("wf-2", "find", "evt-3", input_data={"text": "order #1234"},
              status="failed", error="boom"),
    ])

    _, payload = runs.api_list(workflow_id="wf-1", status="failed", q="order #1234")

    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]

    # Same query through the success alias: only the completed one remains.
    _, payload = runs.api_list(status="success", q="order #1234")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-2"]


def test_empty_query_means_no_filter(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1"),
        _step("wf-2", "post", "evt-2"),
    ])

    for blank in (None, "", "   "):
        _, payload = runs.api_list(q=blank)
        assert len(payload["runs"]) == 2
        assert payload["paging"]["filtered"] is False


def test_search_surfaces_the_failed_step_error_text(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", status="failed",
              error="connection reset by peer"),
    ])

    _, payload = runs.api_list(q="reset by peer")

    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]


def test_cli_runs_list_passes_search_through(isolated_home, monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"runs": [{"run_id": "wf-1:evt-1", "workflow_id": "wf-1",
                          "status": "failed", "steps": 2,
                          "started_at": "2026-09-25T10:00:00+00:00"}]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    rc = main.main(["runs", "list", "--search", "order #1234"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs?limit=25&q=order+%231234")]
    assert "wf-1:evt-1" in capsys.readouterr().out


def test_cli_runs_list_without_search_sends_no_q(isolated_home, monkeypatch):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return {"runs": []}

    monkeypatch.setattr(commands.api, "call", fake_call)

    main.main(["runs", "list"])

    assert calls == [("GET", "/api/agent/runs?limit=25")]


if __name__ == "__main__":
    pytest.main([__file__])
