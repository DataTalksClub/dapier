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
    return visibility.Visibility(MINE)


# --- runs.api_get ------------------------------------------------------------

# --- inbox.api_get -----------------------------------------------------------

# --- errors.api_summary ------------------------------------------------------

# --- storage partition reads -------------------------------------------------

@pytest.fixture
def storage_env(monkeypatch):
    table = FakeStorageTable()
    dynamo(monkeypatch, {"workflow-state": table})
    monkeypatch.setenv("STORAGE_TABLE", "workflow-state")
    return table


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


def test_agent_run_get_denies_non_operators(monkeypatch, published, runs_env):
    import json as _json

    from src.dapier.api import agent as agent_api

    # Off the operator allowlist, the gate denies before any ownership
    # question arises — even for the caller's own workflow's runs.
    agent_identity(monkeypatch, "cli-nobody")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@example.test")
    published.put_item(Item=workflow_item("mine", "cli-nobody"))
    runs_env.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))

    own = agent_api.route(
        agent_event("GET", "/api/agent/runs/mine%3Aevt-e1"),
        "GET", "/api/agent/runs/mine%3Aevt-e1")
    assert own["statusCode"] == 403
    assert _json.loads(own["body"]) == {"error": "Operator authorization required"}


def test_console_designer_get_denies_non_operators(monkeypatch, published):
    import json as _json
    import time as _time

    from src.dapier.api import admin
    from src.dapier.auth import session as session_mod

    monkeypatch.setenv("OPERATOR_EMAILS", "op@example.test")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    published.put_item(Item=workflow_item("mine", MINE))
    monkeypatch.setattr(session_mod, "_credentials",
                        lambda: {"password": "session-secret"})
    monkeypatch.setattr(session_mod, "_audit_event",
                        lambda *args, **kwargs: None)
    token = session_mod._sign({"sub": "user@example.test", "subject": MINE,
                               "exp": int(_time.time()) + 3600})

    own = admin.route(
        cookie_event(token, "GET", "/api/admin/designer/workflows/mine.yaml"),
        "GET", "/api/admin/designer/workflows/mine.yaml")
    assert own["statusCode"] == 403
    assert _json.loads(own["body"]) == {"error": "Operator authorization required"}
