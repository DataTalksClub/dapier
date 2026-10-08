"""Worker presence: what the Workers page and `dapier workers list` show.

The registry rides the host job protocol (`dapier worker` checks in with
every claim poll and heartbeat), so the tests cover three layers the way
the neighboring host worker / host task tests do: stub tables for the
read model, moto for the lease round-trip, and the auth-patched routes
for /api/admin/workers, /api/agent/workers, and the CLI.
"""
import json
import re
import time
from contextlib import contextmanager

import boto3
import pytest
from moto import mock_aws

from dapier_cli import commands as cli_commands
from dapier_cli import main as cli_main
from src.dapier import headless_worker, host_jobs, host_tasks, host_workers
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.auth import session


class Table:
    def __init__(self, items=None):
        self.items = list(items or [])
        self.updates = []

    def get_item(self, Key):
        item = next((item for item in self.items if item["task_id"] == Key["task_id"]), None)
        return {"Item": dict(item)} if item else {}

    def scan(self, **_kwargs):
        return {"Items": [dict(item) for item in self.items]}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        # Apply SET assignments well enough for the assertions below.
        item = next((item for item in self.items
                     if item["task_id"] == kwargs["Key"]["task_id"]), None)
        if item is None:
            item = {"task_id": kwargs["Key"]["task_id"]}
            self.items.append(item)
        names = kwargs.get("ExpressionAttributeNames", {})
        values = kwargs.get("ExpressionAttributeValues", {})
        expression = kwargs["UpdateExpression"].split("SET ", 1)[1]
        for target, source in _assignments(expression):
            if source.startswith("if_not_exists("):
                source = source[len("if_not_exists("):-1].split(", ")[1]
                if target in item:
                    continue
            key = names.get(target, target)
            item[key] = values[source] if source.startswith(":") else item.get(key)


def _assignments(expression):
    """Split ``a = :x, b = if_not_exists(b, :y)`` on top-level commas."""
    parts, depth, current = [], 0, ""
    for char in expression:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.strip().split(" = ", 1) for part in parts if part.strip()]


@pytest.fixture
def workers_table(monkeypatch):
    table = Table()
    # host_workers binds tasks_table at import; patch both module bindings.
    monkeypatch.setattr(host_tasks, "tasks_table", lambda table_ref=None: table_ref or table)
    monkeypatch.setattr(host_workers, "tasks_table", lambda table_ref=None: table_ref or table)
    return table


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
    monkeypatch.setattr(session, "require_operator", lambda event: ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: {"emitted": (args, kwargs)})


def meta(worker_id="ip-10-0-0-7-4242-deadbeef"):
    return {"worker_id": worker_id, "hostname": "ip-10-0-0-7",
            "pid": 4242, "workspace_root": "/home/alexey/dapier-ws"}


# ---- Presence registry ----

def test_checkin_creates_row_then_refreshes_without_resetting_started(workers_table):
    host_workers.checkin("token:host", meta(), table_ref=workers_table, now=1000)
    row = workers_table.get_item(Key={"task_id": "worker:ip-10-0-0-7-4242-deadbeef"})["Item"]
    assert row["kind"] == "worker"
    # The id is more than the row key: the projection reads the attribute.
    assert row["worker_id"] == "ip-10-0-0-7-4242-deadbeef"
    assert row["started_at"] == 1000 and row["last_seen"] == 1000
    assert row["hostname"] == "ip-10-0-0-7" and row["pid"] == 4242
    assert row["owner"] == "token:host"
    assert row["expires_at"] == 1000 + host_workers.TTL_SECONDS
    host_workers.checkin("token:host", meta(), table_ref=workers_table, now=2000)
    row = workers_table.get_item(Key={"task_id": "worker:ip-10-0-0-7-4242-deadbeef"})["Item"]
    assert row["started_at"] == 1000 and row["last_seen"] == 2000


def test_checkin_marks_the_running_task_and_clears_it_on_finish(workers_table):
    host_workers.checkin("token:host", meta(), task_id="agent:flow:e1:run",
                         table_ref=workers_table, now=1000)
    row = workers_table.get_item(Key={"task_id": "worker:ip-10-0-0-7-4242-deadbeef"})["Item"]
    assert row["current_task_id"] == "agent:flow:e1:run"
    host_workers.checkin("token:host", meta(), finished=("agent:flow:e1:run", "succeeded"),
                         table_ref=workers_table, now=2000)
    row = workers_table.get_item(Key={"task_id": "worker:ip-10-0-0-7-4242-deadbeef"})["Item"]
    assert row["current_task_id"] == ""
    assert row["last_task_id"] == "agent:flow:e1:run" and row["last_status"] == "succeeded"


def test_workers_ahead_of_the_window_are_offline(workers_table):
    workers_table.items = [
        {"task_id": "worker:fresh", "kind": "worker", "worker_id": "fresh",
         "last_seen": 990, "started_at": 900},
        {"task_id": "worker:gone", "kind": "worker", "worker_id": "gone",
         "last_seen": 500, "started_at": 400},
        # A task row, not a worker: never surfaces in this list.
        {"task_id": "agent:flow:e1:run", "kind": "agent", "status": "queued",
         "created_at": 1},
    ]
    status, payload = host_workers.api_list(table_ref=workers_table, now=1000)
    assert status == 200
    workers = payload["workers"]
    assert [worker["worker_id"] for worker in workers] == ["fresh", "gone"]
    assert workers[0]["active"] is True and workers[1]["active"] is False
    assert payload["active_window"] == host_workers.ACTIVE_WINDOW_SECONDS
    # The read model stays flat and public: no lease internals.
    assert all("receipt_handle" not in worker and "kind" not in worker
               for worker in workers)


def test_rows_without_the_id_attribute_fall_back_to_the_row_key(workers_table):
    workers_table.items = [
        # Written before the fix that stored worker_id as an attribute.
        {"task_id": "worker:legacy-1", "kind": "worker", "last_seen": 990,
         "started_at": 900},
    ]
    status, payload = host_workers.api_list(table_ref=workers_table, now=1000)
    assert status == 200
    assert [worker["worker_id"] for worker in payload["workers"]] == ["legacy-1"]


def test_meta_of_ignores_old_workers_and_bare_bodies():
    assert host_workers.meta_of(None) is None
    assert host_workers.meta_of({}) is None
    assert host_workers.meta_of({"worker": {}}) is None
    assert host_workers.meta_of({"worker": {"worker_id": "w1"}})["worker_id"] == "w1"
    assert host_workers.meta_of({"worker": {"worker_id": "w1", "pid": "x"}})["pid"] is None


# ---- Check-ins ride the lease protocol ----

@contextmanager
def lease_fixture():
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="eu-west-1").create_table(
            TableName="host-tasks",
            KeySchema=[{"AttributeName": "task_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "task_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        queue = boto3.client("sqs", region_name="eu-west-1")
        url = queue.create_queue(QueueName="host-jobs")["QueueUrl"]
        yield table, queue, url


def test_claim_checks_in_and_marks_the_task_it_leased():
    with lease_fixture() as (table, queue, url):
        message = {"kind": "agent", "task_id": "agent:flow:event:run",
                   "engine": "claude", "workspace": "", "prompt": "write a draft"}
        table.put_item(Item={**message, "status": "queued", "created_at": 1,
                             "notify_to": ""})
        queue.send_message(QueueUrl=url, MessageBody=json.dumps(message))
        body = {"worker": meta()}
        status, payload = host_jobs.claim("token:host", body, table_ref=table,
                                          queue_ref=queue, queue_url=url, now=1000)
        assert status == 200 and payload["job"]["task_id"] == "agent:flow:event:run"
        row = table.get_item(Key={"task_id": "worker:ip-10-0-0-7-4242-deadbeef"})["Item"]
        assert row["last_seen"] == 1000
        assert row["current_task_id"] == "agent:flow:event:run"


def test_idle_claim_refreshes_presence_without_older_worker_side_effects():
    with lease_fixture() as (table, queue, url):
        host_jobs.claim("token:host", {"worker": meta()}, table_ref=table,
                        queue_ref=queue, queue_url=url, now=1000)
        host_jobs.claim("token:host", {"worker": meta()}, table_ref=table,
                        queue_ref=queue, queue_url=url, now=1010)
        row = table.get_item(Key={"task_id": "worker:ip-10-0-0-7-4242-deadbeef"})["Item"]
        assert row["last_seen"] == 1010 and row["started_at"] == 1000
        assert "current_task_id" not in row
        # A worker without the identity block still claims: presence is opt-in.
        status, payload = host_jobs.claim("token:host", table_ref=table,
                                          queue_ref=queue, queue_url=url, now=1020)
        assert status == 200 and payload["job"] is None


def test_heartbeat_and_finish_update_the_worker_row():
    with lease_fixture() as (table, queue, url):
        message = {"kind": "agent", "task_id": "agent:flow:event:run",
                   "prompt": "write a draft"}
        table.put_item(Item={**message, "status": "queued", "created_at": 1,
                             "notify_to": ""})
        queue.send_message(QueueUrl=url, MessageBody=json.dumps(message))
        job = host_jobs.claim("token:host", {"worker": meta()}, table_ref=table,
                              queue_ref=queue, queue_url=url, now=1000)[1]["job"]
        assert host_jobs.heartbeat({**job, "worker": meta()}, "token:host",
                                   table_ref=table, queue_ref=queue,
                                   queue_url=url, now=1040)[0] == 200
        row = table.get_item(Key={"task_id": "worker:ip-10-0-0-7-4242-deadbeef"})["Item"]
        assert row["last_seen"] == 1040
        assert host_jobs.finish({**job, "worker": meta(), "status": "succeeded",
                                 "summary": "Done", "exit_code": 0}, "token:host",
                                table_ref=table, queue_ref=queue,
                                queue_url=url, now=1080)[0] == 200
        row = table.get_item(Key={"task_id": "worker:ip-10-0-0-7-4242-deadbeef"})["Item"]
        assert row["last_seen"] == 1080 and row["current_task_id"] == ""
        assert row["last_status"] == "succeeded"


def test_task_list_hides_worker_rows(workers_table):
    workers_table.items = [
        {"task_id": "agent:flow:e1:run", "kind": "agent", "status": "queued",
         "created_at": 100},
        {"task_id": "worker:w1", "kind": "worker", "worker_id": "w1",
         "last_seen": 100, "started_at": 90},
    ]
    assert [task["task_id"] for task in host_tasks.api_list()[1]["tasks"]] == \
        ["agent:flow:e1:run"]


# ---- The worker process checks in ----

def test_worker_identity_names_the_host_process():
    worker_id, identity = headless_worker.identity("/home/alexey/dapier-ws")
    assert identity["worker_id"] == worker_id
    assert worker_id.startswith(f"{identity['hostname']}-")
    assert re.fullmatch(r".*-\d+-[0-9a-f]{8}", worker_id)
    assert identity["workspace_root"] == "/home/alexey/dapier-ws"


def test_serve_checks_in_on_claim_and_finish(tmp_path, capsys):
    calls = []

    class Api:
        def call(self, operation, body=None):
            calls.append((operation, body))
            if operation == "claim":
                return {"job": None}
            return {}

    assert headless_worker.serve(workspace_root=tmp_path, once=True, api=Api()) is None
    operation, body = calls[0]
    assert operation == "claim"
    assert body["worker"]["worker_id"]
    assert body["worker"]["workspace_root"] == str(tmp_path)
    out = capsys.readouterr().out
    assert "polling for agent tasks" in out and body["worker"]["worker_id"] in out


def test_run_job_sends_its_identity_with_heartbeat_and_finish(tmp_path):
    calls = []

    class Api:
        def call(self, operation, body=None):
            calls.append((operation, body))
            return {}

    import subprocess
    import sys

    def fake_popen(_argv, **kwargs):
        return subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stdin.read(); print('ok')"], **kwargs,
        )

    headless_worker.run_job(
        {"task_id": "agent:flow:event:run", "lease_id": "lease-1",
         "engine": "claude", "workspace": "", "prompt": "write a draft"},
        Api(), workspace_root=tmp_path, worker_id="w-1",
        popen=fake_popen, sleep=lambda _: None,
    )
    finish = calls[-1]
    assert finish[0] == "finish" and finish[1]["worker"] == {"worker_id": "w-1"}


# ---- Console route (/api/admin) ----

def test_admin_workers_require_a_session(monkeypatch):
    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(admin_request("GET", "/api/admin/workers"),
                           "GET", "/api/admin/workers")
    assert response["statusCode"] == 401


def test_admin_workers_list_through_the_shared_list(workers_table, operator_session):
    now = int(time.time())
    workers_table.items = [
        {"task_id": "worker:w1", "kind": "worker", "worker_id": "w1",
         "last_seen": now - 10, "started_at": now - 900},
    ]
    response = admin.route(admin_request("GET", "/api/admin/workers"),
                           "GET", "/api/admin/workers")
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["workers"][0]["worker_id"] == "w1"
    assert payload["workers"][0]["active"] is True


# ---- CLI route (/api/agent) ----

def test_agent_workers_are_operator_gated(monkeypatch, agent_identity):
    monkeypatch.setenv("OPERATOR_SUBJECTS", "someone-else")
    response = agent_api.route(agent_request("GET", "/api/agent/workers"),
                               "GET", "/api/agent/workers")
    assert response["statusCode"] == 403


def test_agent_workers_list_through_the_shared_list(workers_table, agent_identity):
    now = int(time.time())
    workers_table.items = [
        {"task_id": "worker:w1", "kind": "worker", "worker_id": "w1",
         "hostname": "ip-10-0-0-7", "pid": 4242, "last_seen": now - 10,
         "started_at": now - 900},
    ]
    response = agent_api.route(agent_request("GET", "/api/agent/workers"),
                               "GET", "/api/agent/workers")
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["workers"] == [{
        "worker_id": "w1", "hostname": "ip-10-0-0-7", "pid": 4242,
        "workspace_root": None, "owner": None, "started_at": now - 900,
        "last_seen": now - 10,
        "current_task_id": None, "last_task_id": None, "last_status": None,
        "active": True, "capabilities": [], "engine": "claude",
    }]


# ---- CLI client ----

def test_cli_workers_list_calls_the_agent_route(monkeypatch, capsys):
    seen = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        seen.append((method, path))
        return {"workers": [
            {"worker_id": "ip-1-111-aaaa", "hostname": "ip-1", "pid": 111,
             "active": True, "current_task_id": "agent:flow:e1:run",
             "last_task_id": None, "last_status": None},
            {"worker_id": "ip-2-222-bbbb", "hostname": "ip-2", "pid": 222,
             "active": False, "current_task_id": None,
             "last_task_id": "agent:old:e:r", "last_status": "succeeded"},
        ]}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_main.main(["workers", "list"]) == 0
    assert seen == [("GET", "/api/agent/workers")]
    out = capsys.readouterr().out
    assert "active" in out and "offline" in out
    assert "ip-1-111-aaaa" in out and "agent:flow:e1:run" in out
    assert "last agent:old:e:r -> succeeded" in out


def test_cli_workers_list_says_how_to_start_one(monkeypatch, capsys):
    monkeypatch.setattr(cli_commands.api, "call",
                        lambda *args, **kwargs: {"workers": []})
    assert cli_main.main(["workers", "list"]) == 0
    out = capsys.readouterr().out
    assert "dapier worker" in out and "queued" in out
