"""Bulk replay-failed: enumerate a workflow's latest failed runs and re-run
them through the same machinery as a single replay, per surface (domain,
agent, admin, CLI)."""
import json
import time

import boto3
import pytest

from dapier_cli import commands, main
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import runs
from src.dapier.auth import session


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
          error=None, input_data=None, run_id=None):
    item = {
        "execution_id": f"{workflow_id}:{action_id}:{event_id}",
        "run_id": run_id or f"{workflow_id}:{event_id}",
        "workflow_id": workflow_id,
        "action_id": action_id,
        "status": status,
        "started_at": started,
        "finished_at": started,
        "connector": "email",
        "event_type": "message.received",
        "correlation_id": event_id,
        "expires_at": 1789000000,
        "input": input_data,
    }
    if error:
        item["error"] = error
    return item


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


# --- domain ----------------------------------------------------------------


def test_replay_failed_replays_failures_and_skips_the_rest(monkeypatch):
    _configure(monkeypatch, [
        _step("wf-1", "post", "evt-1", status="failed", error="boom",
              input_data={"subject": "invoice"}),
        _step("wf-1", "post", "evt-2", status="failed", error="boom",
              input_data={"truncated": True, "preview": '{"sub'}),
        _step("wf-1", "post", "evt-3", status="failed", error="boom", input_data=None),
        _step("wf-1", "post", "evt-4"),  # completed: not enumerated at all
        _step("wf-2", "post", "evt-5", status="failed", error="boom",
              input_data={"subject": "other workflow"}),
    ])
    queue = _configure_queue(monkeypatch)

    status, payload = runs.api_replay_failed("wf-1")

    assert status == 202
    assert payload["accepted"] is True
    assert payload["workflow_id"] == "wf-1"
    assert payload["replayed"] == 1
    assert payload["skipped"] == 2
    by_id = {result["run_id"]: result for result in payload["runs"]}
    assert by_id["wf-1:evt-1"]["replayed"] is True
    assert by_id["wf-1:evt-1"]["new_run_id"].startswith("wf-1:replay-")
    assert by_id["wf-1:evt-2"]["replayed"] is False
    assert "too large" in by_id["wf-1:evt-2"]["reason"]
    assert by_id["wf-1:evt-3"]["replayed"] is False
    assert "cannot be replayed" in by_id["wf-1:evt-3"]["reason"]
    assert len(queue.messages) == 1  # evt-4 and wf-2 never went out
    assert json.loads(queue.messages[0]["MessageBody"])["data"] == {"subject": "invoice"}


def test_replay_failed_without_a_workflow_is_400(monkeypatch):
    _configure(monkeypatch, [])
    _configure_queue(monkeypatch)
    assert runs.api_replay_failed("  ")[0] == 400


def test_replay_failed_on_a_workflow_without_failures_is_empty(monkeypatch):
    _configure(monkeypatch, [_step("wf-1", "post", "evt-1")])
    _configure_queue(monkeypatch)

    status, payload = runs.api_replay_failed("wf-1")

    assert status == 202
    assert payload["runs"] == []
    assert payload["replayed"] == 0


# --- agent route -----------------------------------------------------------


def _configure_agent(monkeypatch, items, operator=True):
    agent_api.reset_rate_limits()
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setenv("OPERATOR_EMAILS",
                       "op@datatalks.club" if operator else "someone-else@datatalks.club")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {"sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setattr(agent_api.runs, "_table", lambda: FakeExecTable(items))


def _bearer_event(body=None):
    request = {"headers": {"host": "dapier.example.test", "authorization": "Bearer dtc-token"},
               "cookies": []}
    if body is not None:
        request["body"] = json.dumps(body)
    return request


def test_agent_replay_failed_over_bearer(monkeypatch):
    _configure_agent(monkeypatch, [
        _step("wf-1", "post", "evt-1", status="failed", error="boom",
              input_data={"subject": "invoice"}),
        _step("wf-1", "post", "evt-2", status="failed", error="boom", input_data=None),
    ])
    queue = _configure_queue(monkeypatch)

    response = agent_api.route(_bearer_event({"workflow_id": "wf-1"}),
                               "POST", "/api/agent/runs/replay-failed")

    assert response["statusCode"] == 202
    body = json.loads(response["body"])
    assert body["accepted"] is True
    assert body["replayed"] == 1
    assert body["skipped"] == 1
    assert queue.messages[0]["QueueUrl"] == "https://sqs.test/events"


def test_agent_replay_failed_requires_operator(monkeypatch):
    _configure_agent(monkeypatch, [], operator=False)

    response = agent_api.route(_bearer_event({"workflow_id": "wf-1"}),
                               "POST", "/api/agent/runs/replay-failed")

    assert response["statusCode"] == 403


def test_agent_replay_failed_rejects_bad_json(monkeypatch):
    _configure_agent(monkeypatch, [])

    response = agent_api.route({"headers": {"authorization": "Bearer dtc-token"},
                                "cookies": [], "body": "{not json"},
                               "POST", "/api/agent/runs/replay-failed")

    assert response["statusCode"] == 400


# --- admin route -----------------------------------------------------------


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


def _admin_request(body=None, cookies=None):
    return {
        "requestContext": {"http": {"method": "POST", "path": "/api/admin/runs/replay-failed"}},
        "headers": {"host": "dapier.example.test", "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
        "queryStringParameters": None,
        "body": None if body is None else json.dumps(body),
    }


def test_admin_replay_failed(monkeypatch):
    cookies = _configure_admin(monkeypatch, [
        _step("wf-1", "post", "evt-1", status="failed", error="boom",
              input_data={"subject": "invoice"}),
    ])
    queue = _configure_queue(monkeypatch)

    response = admin.route(_admin_request({"workflow_id": "wf-1"}, cookies=cookies),
                           "POST", "/api/admin/runs/replay-failed")

    assert response["statusCode"] == 202
    body = json.loads(response["body"])
    assert body["accepted"] is True
    assert body["replayed"] == 1
    assert len(queue.messages) == 1


def test_admin_replay_failed_rejects_bad_json(monkeypatch):
    cookies = _configure_admin(monkeypatch, [])

    request = _admin_request(None, cookies=cookies)
    request["body"] = "{not json"
    assert admin.route(request, "POST", "/api/admin/runs/replay-failed")["statusCode"] == 400


# --- CLI -------------------------------------------------------------------


def test_cli_replay_failed_posts_the_workflow(monkeypatch, capsys):
    seen = {}

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.update(method=method, path=path, body=body)
        return {"accepted": True, "workflow_id": "wf-1", "replayed": 1, "skipped": 1,
                "runs": [{"run_id": "wf-1:evt-1", "replayed": True, "new_run_id": "wf-1:replay-1"},
                         {"run_id": "wf-1:evt-2", "replayed": False, "reason": "too large"}]}

    monkeypatch.setattr(commands.api, "call", fake_call)

    assert commands.runs_replay_failed("https://api.example.test", "wf-1") == 0
    assert (seen["method"], seen["path"]) == ("POST", "/api/agent/runs/replay-failed")
    assert seen["body"] == {"workflow_id": "wf-1"}
    out, _ = capsys.readouterr()
    assert "Replay accepted for 1 unresolved failed run(s) of wf-1; 1 skipped." in out
    assert "wf-1:evt-1: replayed as wf-1:replay-1" in out
    assert "wf-1:evt-2: skipped (too large)" in out


def test_main_runs_replay_failed_calls_through(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("DAPIER_API_URL", raising=False)
    seen = {}

    def fake_replay_failed(api_url, workflow_id, debug=False):
        seen["workflow_id"] = workflow_id
        return 0

    monkeypatch.setattr(commands, "runs_replay_failed", fake_replay_failed)
    assert main.main(["runs", "replay-failed", "wf-1"]) == 0
    assert seen["workflow_id"] == "wf-1"


if __name__ == "__main__":
    pytest.main([__file__])
