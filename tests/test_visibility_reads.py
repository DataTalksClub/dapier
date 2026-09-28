"""G17 Phase 2 follow-up: the per-item reads that list filtering round
missed — one run (runs.api_get), the trigger sample, one inbox event
(inbox.api_get), the errors summary, and a workflow's storage partition —
taking the same ``visible`` scope their lists already take.

Rule under test: a hidden item answers exactly like a missing one (404 /
empty — never a distinct denial that would reveal it exists); anything
with no owner — a run whose workflow vanished, an event nothing matched —
stays visible to everyone (the defensive read rule).
"""
from datetime import datetime, timezone

import boto3
import pytest

from src.dapier.api import errors as errors_api
from src.dapier.api import runs as runs_api
from src.dapier.api import storage as storage_api
from src.dapier.auth import visibility
from src.dapier.engine.actions import storage as storage_actions
from src.dapier.triggers import inbox as inbox_store
from src.dapier.triggers import published_workflows

from test_storage_actions import FakeStorageTable

MINE, THEIRS = "subject-1", "subject-2"


# --- stubs and fixtures ------------------------------------------------------

class PublishedTable:
    """The published-workflows store: whole-item get/put/delete over scan."""

    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[Item["workflow_id"]] = Item

    def get_item(self, Key):
        item = self.items.get(Key["workflow_id"])
        return {"Item": item} if item else {}

    def delete_item(self, Key):
        self.items.pop(Key["workflow_id"], None)

    def scan(self, **_):
        return {"Items": list(self.items.values())}


class ExecTable:
    """Scan serves the whole window; query is the runs-by-run-id GSI."""

    def __init__(self):
        self.items = []

    def put_item(self, **kwargs):
        self.items.append(kwargs["Item"])

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def query(self, **kwargs):
        values = list((kwargs.get("ExpressionAttributeValues") or {}).values())
        wanted = values[0] if values else None
        return {"Items": [item for item in self.items
                          if item.get("run_id") == wanted]}


class InboxTable:
    def __init__(self):
        self.items = {}

    def put_item(self, **kwargs):
        self.items[kwargs["Item"]["inbox_id"]] = kwargs["Item"]

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"]["inbox_id"])
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def dynamo(monkeypatch, tables):
    class Dynamo:
        def Table(self, name):
            return tables[name]

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return tables


@pytest.fixture
def published(monkeypatch):
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = PublishedTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    return table


@pytest.fixture
def runs_env(monkeypatch):
    table = ExecTable()
    dynamo(monkeypatch, {"executions": table})
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    return table


def workflow_item(workflow_id, owner):
    return {
        "workflow_id": workflow_id,
        "file": f"{workflow_id}.yaml",
        "workflow": {"id": workflow_id, "enabled": True,
                     "trigger": {"connector": "email", "event": "message.received"},
                     "actions": [{"id": "a1", "type": "webhook",
                                  "url": "https://example.test/hook"}]},
        "enabled": True,
        "published_by": owner,
        "owner": owner,
        "revision": 1,
    }


def execution(workflow_id, event_id, started, *, status="completed", **extra):
    item = {
        "execution_id": f"{workflow_id}:a1:{event_id}",
        "run_id": f"{workflow_id}:evt-{event_id}",
        "workflow_id": workflow_id,
        "action_id": "a1", "action_type": "webhook", "status": status,
        "connector": "email", "event_type": "message.received",
        "started_at": started, "finished_at": started,
        "duration_ms": 5, "expires_at": 9999999999,
    }
    item.update(extra)
    return item


def viewer():
    return visibility.for_role(MINE, "viewer")


# --- runs.api_get ------------------------------------------------------------

def test_run_get_scopes_to_visible_workflows(monkeypatch, published, runs_env):
    seed = published
    seed.put_item(Item=workflow_item("mine", MINE))
    seed.put_item(Item=workflow_item("theirs", THEIRS))
    runs_env.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))
    runs_env.put_item(Item=execution("theirs", "e2", "2026-09-28T09:00:00+00:00"))
    # A run whose workflow no longer exists: audit duty keeps it visible.
    runs_env.put_item(Item=execution("gone-wf", "e3", "2026-09-28T08:00:00+00:00"))

    operator = runs_api.api_get("mine:evt-e1")
    assert operator[0] == 200
    assert runs_api.api_get("theirs:evt-e2")[0] == 200

    scope = viewer()
    assert runs_api.api_get("mine:evt-e1", visible=scope)[0] == 200
    assert runs_api.api_get("gone-wf:evt-e3", visible=scope)[0] == 200
    hidden = runs_api.api_get("theirs:evt-e2", visible=scope)
    missing = runs_api.api_get("nope:evt-none")
    assert hidden == missing  # a hidden run reads exactly like a missing one


def test_trigger_sample_scopes_to_visible_workflows(monkeypatch, published, runs_env):
    published.put_item(Item=workflow_item("mine", MINE))
    published.put_item(Item=workflow_item("theirs", THEIRS))
    runs_env.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00",
                                     input={"body": "hello"}))

    scope = viewer()
    own = runs_api.api_trigger_sample("mine", visible=scope)
    assert own[0] == 200
    assert own[1]["source"] == "history"
    assert own[1]["data"] == {"body": "hello"}
    hidden = runs_api.api_trigger_sample("theirs", visible=scope)
    assert hidden[0] == 404


# --- inbox.api_get -----------------------------------------------------------

def test_inbox_get_scopes_to_matched_workflows(monkeypatch, published):
    published.put_item(Item=workflow_item("mine", MINE))
    published.put_item(Item=workflow_item("theirs", THEIRS))
    table = InboxTable()
    table.put_item(Item={"inbox_id": "ev-mine", "connector": "email",
                         "event": "message.received", "matched": ["mine"],
                         "data": {"body": "hello"}})
    table.put_item(Item={"inbox_id": "ev-theirs", "connector": "email",
                         "event": "message.received", "matched": ["theirs"],
                         "data": {"body": "secret"}})
    table.put_item(Item={"inbox_id": "ev-none", "connector": "email",
                         "event": "message.received", "matched": [],
                         "data": {"body": "unmatched"}})

    operator = inbox_store.api_get("ev-theirs", table_ref=table)
    assert operator[0] == 200

    scope = viewer()
    assert inbox_store.api_get("ev-mine", table_ref=table, visible=scope)[0] == 200
    # Matched nothing = nobody's row: the defensive rule keeps it visible.
    assert inbox_store.api_get("ev-none", table_ref=table, visible=scope)[0] == 200
    hidden = inbox_store.api_get("ev-theirs", table_ref=table, visible=scope)
    missing = inbox_store.api_get("ev-gone", table_ref=table)
    assert hidden == missing


# --- errors.api_summary ------------------------------------------------------

def test_error_summary_scopes_counts_to_visible_workflows(monkeypatch, published, runs_env):
    published.put_item(Item=workflow_item("mine", MINE))
    published.put_item(Item=workflow_item("theirs", THEIRS))
    runs_env.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00",
                                     status="failed", error="boom"))
    runs_env.put_item(Item=execution("mine", "e2", "2026-09-28T09:00:00+00:00",
                                     status="failed", error="also boom"))
    runs_env.put_item(Item=execution("theirs", "e3", "2026-09-28T08:00:00+00:00",
                                     status="failed", error="theirs"))
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)

    operator = errors_api.api_summary(days=7, now=now)
    assert operator[1]["total_failed_runs"] == 3
    assert [row["workflow_id"] for row in operator[1]["workflows"]] == [
        "mine", "theirs"]

    scope = viewer()
    scoped = errors_api.api_summary(days=7, now=now, visible=scope)
    assert scoped[1]["total_failed_runs"] == 2
    assert [row["workflow_id"] for row in scoped[1]["workflows"]] == ["mine"]
    assert "bounded" not in scoped[1]


# --- storage partition reads -------------------------------------------------

@pytest.fixture
def storage_env(monkeypatch):
    table = FakeStorageTable()
    dynamo(monkeypatch, {"workflow-state": table})
    monkeypatch.setenv("STORAGE_TABLE", "workflow-state")
    return table


def test_storage_partition_reads_hide_other_owners(monkeypatch, published, storage_env):
    published.put_item(Item=workflow_item("mine", MINE))
    published.put_item(Item=workflow_item("theirs", THEIRS))
    storage_actions.kv_set("mine", "shared-key", "mine-value")
    storage_actions.kv_set("theirs", "shared-key", "theirs-value")

    operator = storage_api.get("theirs", "shared-key")
    assert operator[0] == 200

    scope = viewer()
    assert storage_api.get("mine", "shared-key", visible=scope)[1]["value"] == "mine-value"
    # A hidden partition answers exactly like an empty one: the same 404 a
    # workflow with nothing stored returns (an unknown workflow id resolves
    # to no owner, so the defensive rule reads it — and finds nothing).
    hidden = storage_api.get("theirs", "shared-key", visible=scope)
    empty = storage_api.get("ghost-wf", "shared-key", visible=scope)
    assert hidden == empty

    own_find = storage_api.find("mine", "", 10, visible=scope)
    assert own_find[1]["count"] == 1
    hidden_find = storage_api.find("theirs", "", 10, visible=scope)
    assert hidden_find[1]["count"] == 0
    assert hidden_find[1]["items"] == []


# --- route-level: the threading reaches the caller ---------------------------

def agent_event(method, path):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "authorization": "Bearer dtc-token"},
        "cookies": [],
    }


def cookie_event(token, method="GET", path="/api/admin/overview"):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [f"dapier_session={token}"],
    }


def agent_identity(monkeypatch, sub):
    from src.dapier.api import agent as agent_api
    from src.dapier.auth import session as session_mod

    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("OPERATOR_SUBJECTS", raising=False)
    monkeypatch.delenv("OPERATOR_EMAILS", raising=False)
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {"sub": sub})
    monkeypatch.setattr(agent_api, "audit", type("Audit", (), {
        "emit": staticmethod(lambda *args, **kwargs: None)}))
    monkeypatch.setattr(session_mod, "_credentials",
                        lambda: {"password": "session-secret"})


def test_agent_run_get_scopes_the_cli_viewer(monkeypatch, published, runs_env):
    import json as _json

    from src.dapier.api import agent as agent_api

    agent_identity(monkeypatch, "cli-viewer")
    monkeypatch.setenv("ROLE_ASSIGNMENTS_TABLE", "role-assignments")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    roles = {}
    roles["cli-viewer"] = {"identity": "cli-viewer", "role": "viewer"}
    published.put_item(Item=workflow_item("mine", "cli-viewer"))
    published.put_item(Item=workflow_item("theirs", "cli-other"))
    runs_env.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))
    runs_env.put_item(Item=execution("theirs", "e2", "2026-09-28T09:00:00+00:00"))

    class RoleTable:
        def get_item(self, **kwargs):
            item = roles.get(kwargs["Key"]["identity"])
            return {"Item": dict(item)} if item else {}

    class Dynamo:
        def Table(self, name):
            if name == "executions":
                return runs_env
            if name == "role-assignments":
                return RoleTable()
            return type("T", (), {"get_item": lambda self, **k: {}})()

    import boto3 as _boto3
    monkeypatch.setattr(_boto3, "resource", lambda service: Dynamo())

    own = agent_api.route(
        agent_event("GET", "/api/agent/runs/mine%3Aevt-e1"),
        "GET", "/api/agent/runs/mine%3Aevt-e1")
    assert _json.loads(own["body"])["run"]["workflow_id"] == "mine"
    hidden = agent_api.route(
        agent_event("GET", "/api/agent/runs/theirs%3Aevt-e2"),
        "GET", "/api/agent/runs/theirs%3Aevt-e2")
    assert _json.loads(hidden["body"]) == {"error": "Run not found"}


def test_console_designer_get_scopes_the_viewer(monkeypatch, published):
    import json as _json
    import time as _time

    from src.dapier.api import admin
    from src.dapier.auth import session as session_mod

    monkeypatch.setenv("OPERATOR_EMAILS", "op@example.test")
    monkeypatch.setenv("ROLE_ASSIGNMENTS_TABLE", "role-assignments")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    published.put_item(Item=workflow_item("mine", MINE))
    published.put_item(Item=workflow_item("theirs", THEIRS))
    roles = {"user@example.test": {"identity": "user@example.test",
                                   "role": "viewer"}}

    class RoleTable:
        def get_item(self, **kwargs):
            item = roles.get(kwargs["Key"]["identity"])
            return {"Item": dict(item)} if item else {}

    class Dynamo:
        def Table(self, name):
            if name == "published-test":
                return published
            if name == "role-assignments":
                return RoleTable()
            return type("T", (), {"get_item": lambda self, **k: {}})()

    import boto3 as _boto3
    monkeypatch.setattr(_boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(session_mod, "_credentials",
                        lambda: {"password": "session-secret"})
    token = session_mod._sign({"sub": "user@example.test", "subject": MINE,
                               "exp": int(_time.time()) + 3600})

    own = admin.route(
        cookie_event(token, "GET", "/api/admin/designer/workflows/mine.yaml"),
        "GET", "/api/admin/designer/workflows/mine.yaml")
    assert _json.loads(own["body"])["workflow"]["id"] == "mine"
    hidden = admin.route(
        cookie_event(token, "GET", "/api/admin/designer/workflows/theirs.yaml"),
        "GET", "/api/admin/designer/workflows/theirs.yaml")
    assert _json.loads(hidden["body"]) == {"error": "no such workflow: theirs.yaml"}


# --- route-level: the threading reaches the caller ---------------------------

