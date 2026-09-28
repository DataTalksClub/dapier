"""Host task reads: the shared list behind /api/admin/agent-tasks,
/api/agent/agent-tasks, and `dapier agent-tasks list`.

Stub-table style like the neighboring host worker tests: the DynamoDB table
is a list behind a scan(), and the auth gates are patched at the session
boundary exactly like test_copilot.py does.
"""
import json

import pytest

from dapier_cli import commands as cli_commands
from dapier_cli import main as cli_main
from src.dapier import host_tasks
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.auth import session


class Table:
    def __init__(self, items=None):
        self.items = list(items or [])

    def scan(self, **_kwargs):
        return {"Items": [dict(item) for item in self.items]}


@pytest.fixture
def tasks_table(monkeypatch):
    table = Table()
    monkeypatch.setattr(host_tasks, "tasks_table", lambda table_ref=None: table_ref or table)
    return table


def task_row(task_id, created_at, status="starting", **extra):
    """A row as engine.actions.agent writes it and host_worker mutates it."""
    row = {"task_id": task_id, "kind": "agent", "status": status,
           "created_at": created_at, "prompt": "do the thing", "workspace": "/work"}
    row.update(extra)
    return row


def api_request(method, path, query=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "queryStringParameters": query,
        "body": None,
    }


def admin_request(method, path, query=None):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "queryStringParameters": query,
        "body": None,
    }


def agent_request(method, path, query=None, token="dtc-token"):
    request = admin_request(method, path, query)
    request["headers"]["authorization"] = f"Bearer {token}"
    return request


@pytest.fixture
def agent_identity(monkeypatch):
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("OPERATOR_SUBJECTS", raising=False)
    monkeypatch.delenv("OPERATOR_EMAILS", raising=False)
    monkeypatch.setattr(
        agent_api, "verify_id_token", lambda token, audience=None: {"sub": "agent-op"},
    )
    monkeypatch.setattr(agent_api, "audit", type("Audit", (), {
        "emit": staticmethod(lambda *args, **kwargs: None),
    }))


@pytest.fixture
def operator_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: True)
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(session, "require_role",
                        lambda event, minimum="operator": ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: {"emitted": (args, kwargs)})


# ---- Domain list ----

def test_list_is_newest_first_limited_and_projected(tasks_table):
    tasks_table.items = [
        task_row("agent:old-flow:e1:wake", 100, status="started",
                 sent_at=105, session_id="s1", tag="agent-1"),
        task_row("agent:new-flow:e3:wake", 300, status="started", sent_at=305),
        task_row("agent:mid-flow:e2:wake", 200),
    ]
    status, payload = host_tasks.api_list(limit=2)
    assert status == 200
    ids = [item["task_id"] for item in payload["tasks"]]
    assert ids == ["agent:new-flow:e3:wake", "agent:mid-flow:e2:wake"]
    newest = payload["tasks"][0]
    assert newest["workflow"] == "new-flow"
    assert newest["status"] == "started"
    assert newest["sent_at"] == 305
    # The row's prompt never leaves the table through the read surface.
    assert all("prompt" not in item for item in payload["tasks"])


def test_status_filter_is_case_insensitive(tasks_table):
    tasks_table.items = [
        task_row("agent:f:e1:wake", 100, status="starting", error="workspace is not a directory: /work"),
        task_row("agent:f:e2:wake", 200, status="started"),
    ]
    status, payload = host_tasks.api_list(status="STARTED")
    assert status == 200
    assert [item["task_id"] for item in payload["tasks"]] == ["agent:f:e2:wake"]
    _, failed = host_tasks.api_list(status="starting")
    assert failed["tasks"][0]["error"].startswith("workspace is not a directory")


# ---- Console route (/api/admin) ----

def test_admin_agent_tasks_require_a_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(admin_request("GET", "/api/admin/agent-tasks"),
                           "GET", "/api/admin/agent-tasks")
    assert response["statusCode"] == 401


def test_admin_agent_tasks_list_through_the_shared_list(tasks_table, operator_session):
    tasks_table.items = [task_row("agent:f:e1:wake", 100, status="started")]
    response = admin.route(admin_request("GET", "/api/admin/agent-tasks",
                                         query={"limit": "1", "status": "started"}),
                           "GET", "/api/admin/agent-tasks")
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["tasks"][0]["task_id"] == "agent:f:e1:wake"


# ---- CLI route (/api/agent) ----

def test_agent_agent_tasks_are_operator_gated(monkeypatch, agent_identity):
    monkeypatch.setenv("OPERATOR_SUBJECTS", "someone-else")
    response = agent_api.route(agent_request("GET", "/api/agent/agent-tasks"),
                               "GET", "/api/agent/agent-tasks")
    assert response["statusCode"] == 403


def test_agent_agent_tasks_list_through_the_shared_list(tasks_table, agent_identity):
    tasks_table.items = [task_row("agent:f:e1:wake", 100, status="started", sent_at=105)]
    response = agent_api.route(
        agent_request("GET", "/api/agent/agent-tasks", query={"status": "started"}),
        "GET", "/api/agent/agent-tasks",
    )
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["tasks"] == [{
        "task_id": "agent:f:e1:wake", "kind": "agent", "engine": None,
        "workspace": "/work", "tag_prefix": None, "status": "started",
        "session_id": None, "tag": None, "error": None,
        "created_at": 100, "sent_at": 105, "workflow": "f",
    }]


# ---- CLI client ----

def test_cli_agent_tasks_list_calls_the_agent_route(monkeypatch, capsys):
    seen = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.append((method, path))
        if len(seen) == 1:
            return {"tasks": [task_row("agent:orders:e9:wake", 300, status="starting",
                                       error="session was not ready for input")]}
        return {"tasks": []}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_main.main(["agent-tasks", "list"]) == 0
    assert cli_main.main(["agent-tasks", "list", "--limit", "5", "--status", "started"]) == 0
    assert seen == [
        ("GET", "/api/agent/agent-tasks"),
        ("GET", "/api/agent/agent-tasks?limit=5&status=started"),
    ]
    out = capsys.readouterr().out
    assert "STATUS" in out and "orders" in out and "agent:orders:e9:wake" in out
    assert "error session was not ready for input" in out
    assert "No host tasks yet" in out
