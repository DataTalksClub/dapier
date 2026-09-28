"""Run-history content search: the list's ``q`` filter matches a run's
recorded step data (input, output, error) and ids case-insensitively, on
the domain list, both operator routes (/api/agent + /api/admin), and the
CLI flag that drives the agent route."""
import json
import time

import boto3
import pytest

from dapier_cli import commands, main
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import runs
from src.dapier.auth import session


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    return tmp_path


class FakeExecTable:
    """One-shot scan of the whole window (DynamoDB pages via the loop cap)."""

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


def _run(workflow_id, event_id, *, status="completed", started="2026-09-25T10:00:00+00:00",
         input_data=None, output=None, error=None):
    item = {
        "execution_id": f"{workflow_id}:post:{event_id}",
        "run_id": f"{workflow_id}:{event_id}",
        "workflow_id": workflow_id,
        "action_id": "post",
        "action_type": "webhook",
        "connector": "email",
        "event_type": "message.received",
        "status": status,
        "started_at": started,
        "finished_at": started,
    }
    if input_data is not None:
        item["input"] = input_data
    if output is not None:
        item["output"] = output
    if error:
        item["error"] = error
    return item


# --- domain list ------------------------------------------------------------


def test_q_matches_a_step_output_case_insensitively(monkeypatch):
    _configure(monkeypatch, [
        _run("wf-1", "evt-1",
             output={"order": {"id": "order-1234", "total": "49.90"}}),
        _run("wf-2", "evt-2", input_data={"text": "unrelated"}),
    ])

    status, payload = runs.api_list(q="ORDER-1234")
    assert status == 200
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]
    assert payload["paging"]["filtered"] is True


def test_q_matches_a_step_input(monkeypatch):
    _configure(monkeypatch, [
        _run("wf-1", "evt-1", input_data={"customer": {"email": "buyer@example.com"}}),
        _run("wf-2", "evt-2", output={"ok": True}),
    ])

    _, payload = runs.api_list(q="buyer@example.com")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]


def test_q_matches_a_step_error(monkeypatch):
    _configure(monkeypatch, [
        _run("wf-1", "evt-1", status="failed", error="ConnectionResetError: peer closed"),
        _run("wf-2", "evt-2", status="completed"),
    ])

    _, payload = runs.api_list(q="connectionreseterror")
    assert [run["run_id"] for run in payload["runs"]] == ["wf-1:evt-1"]


def test_q_without_a_match_returns_an_empty_page(monkeypatch):
    _configure(monkeypatch, [
        _run("wf-1", "evt-1", input_data={"text": "standup notes"}),
        _run("wf-2", "evt-2"),
    ])

    status, payload = runs.api_list(q="no-such-needle")
    assert status == 200
    assert payload["runs"] == []
    assert payload["paging"]["filtered"] is True


def test_q_absent_leaves_the_list_unchanged(monkeypatch):
    _configure(monkeypatch, [
        _run("wf-1", "evt-1", input_data={"text": "standup notes"}),
        _run("wf-2", "evt-2"),
    ])

    baseline_status, baseline = runs.api_list()
    assert baseline_status == 200
    assert [run["run_id"] for run in baseline["runs"]] == ["wf-2:evt-2", "wf-1:evt-1"]
    assert baseline["paging"]["filtered"] is False
    for blank in (None, "", "   "):
        assert runs.api_list(q=blank) == (200, baseline)  # identical payload


# --- agent route (/api/agent/runs, what the CLI drives) ---------------------


def _configure_agent(monkeypatch, items):
    agent_api.reset_rate_limits()
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setattr(agent_api.runs, "_table", lambda: FakeExecTable(items))


def _bearer_event(query=None):
    return {
        "headers": {"host": "dapier.example.test", "authorization": "Bearer dtc-token"},
        "cookies": [],
        "queryStringParameters": query,
    }


def test_agent_runs_route_forwards_q(monkeypatch):
    _configure_agent(monkeypatch, [
        _run("wf-1", "evt-1", output={"order": {"id": "order-1234"}}),
        _run("wf-2", "evt-2"),
    ])

    listed = agent_api.route(_bearer_event({"q": "order-1234"}), "GET", "/api/agent/runs")

    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert [run["run_id"] for run in body["runs"]] == ["wf-1:evt-1"]
    assert body["paging"]["filtered"] is True


# --- admin route (/api/admin/runs, what the console drives) ------------------


def _configure_admin(monkeypatch, items):
    monkeypatch.setattr(session, "_credentials", lambda: {"username": "admin", "password": "pw"})
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})

    class Dynamo:
        def Table(self, _name):
            return FakeExecTable(items)

    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return [f"dapier_session={cookie}"]


def _admin_request(method, path, query=None, cookies=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test", "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
        "queryStringParameters": query,
        "body": None,
    }


def test_admin_runs_route_forwards_q(monkeypatch):
    cookies = _configure_admin(monkeypatch, [
        _run("wf-1", "evt-1"),
        _run("wf-2", "evt-2", input_data={"text": "invoice INV-9"}),
    ])

    listed = admin.route(
        _admin_request("GET", "/api/admin/runs", query={"q": "inv-9"}, cookies=cookies),
        "GET", "/api/admin/runs",
    )

    assert listed["statusCode"] == 200
    body = json.loads(listed["body"])
    assert [run["run_id"] for run in body["runs"]] == ["wf-2:evt-2"]


# --- CLI --------------------------------------------------------------------


def _stub_api(monkeypatch, response):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        return response

    monkeypatch.setattr(commands.api, "call", fake_call)
    return calls


def test_cli_forwards_the_search_flag(isolated_home, monkeypatch, capsys):
    calls = _stub_api(monkeypatch, {
        "runs": [{"run_id": "wf-1:evt-1", "workflow_id": "wf-1", "status": "completed",
                  "steps": 1, "started_at": "2026-09-25T10:00:00+00:00"}],
        "paging": {"next": None, "limit": 25, "filtered": True},
    })

    rc = main.main(["runs", "list", "--search", "order-1234"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs?limit=25&q=order-1234")]
    assert "wf-1:evt-1" in capsys.readouterr().out


def test_cli_without_the_flag_sends_no_q(isolated_home, monkeypatch):
    calls = _stub_api(monkeypatch, {"runs": [],
                                    "paging": {"next": None, "limit": 25, "filtered": False}})

    rc = main.main(["runs", "list"])

    assert rc == 0
    assert calls == [("GET", "/api/agent/runs?limit=25")]


if __name__ == "__main__":
    pytest.main([__file__])
