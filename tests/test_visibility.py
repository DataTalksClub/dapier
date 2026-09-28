"""G17 Phase 2: the visibility helper (auth/visibility.py) and the read
surfaces it filters — designer list, overview, runs, inbox, usage.

Rule under test: operators see everything; a subject sees the workflows it
owns (draft-only rows by drafted_by); anything with no owner — including a
row whose workflow no longer exists — stays visible to everyone.

G17 Phase 3: the same module's owner-or-operator WRITE gate
(ensure_can_write) and the workflow write routes it guards. The rule is the
same verdict with the safe direction on the defensive default: an id
nothing stored claims is a create and stays open, but an item that exists
with no owner stamp is DENIED for non-operators on writes.
"""
import json
import time

import boto3
import pytest
import yaml

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.api import overview as overview_api
from src.dapier.api import runs as runs_api
from src.dapier.auth import session, visibility
from src.dapier.engine import usage as usage_rollup
from src.dapier.triggers import inbox as inbox_store
from src.dapier.triggers import published_workflows

MINE, THEIRS = "subject-1", "subject-2"


# --- stubs and fixtures ------------------------------------------------------

class DictTable:
    def __init__(self, key_fields):
        self.key_fields = key_fields
        self.items = {}

    def _key(self, item_or_key):
        return tuple(item_or_key[field] for field in self.key_fields)

    def put_item(self, **kwargs):
        self.items[self._key(kwargs["Item"])] = kwargs["Item"]

    def get_item(self, **kwargs):
        item = self.items.get(self._key(kwargs["Key"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, **kwargs):
        self.items.pop(self._key(kwargs["Key"]), None)

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())[: kwargs.get("Limit", 100)]}

    def query(self, **kwargs):
        wanted = kwargs["KeyConditionExpression"]._values[-1]
        return {"Items": [item for item in self.items.values()
                          if item.get(self.key_fields[0]) == wanted]}


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


def dynamo(monkeypatch, tables):
    shared = getattr(monkeypatch, "_visibility_tables", None)
    if shared is None:
        shared = {}
        monkeypatch.setattr(monkeypatch, "_visibility_tables", shared, raising=False)
    shared.update(tables)

    class Dynamo:
        def Table(self, name):
            return shared[name]

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return shared


def operator_env(monkeypatch, emails="op@example.test", subjects=""):
    monkeypatch.setenv("OPERATOR_EMAILS", emails)
    monkeypatch.setenv("OPERATOR_SUBJECTS", subjects)


def roles_env(monkeypatch, rows, tables=None):
    """Seed role assignments; ``tables`` (a dict another fixture built) is
    served too, so a test needs exactly one boto3.resource patch."""
    tables = tables if tables is not None else {}
    role_table = tables.setdefault("role-assignments", DictTable(("identity",)))
    for item in rows:
        role_table.put_item(Item=item)
    dynamo(monkeypatch, tables)
    monkeypatch.setenv("ROLE_ASSIGNMENTS_TABLE", "role-assignments")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    return role_table


def session_token(monkeypatch, sub="op@example.test", subject="subject-1"):
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    return session._sign({"sub": sub, "subject": subject, "exp": int(time.time()) + 3600})


def cookie_event(token, method="GET", path="/api/admin/overview"):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [f"dapier_session={token}"],
    }


def agent_event(method, path, sub):
    return {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "authorization": "Bearer dtc-token"},
        "cookies": [],
    }


def agent_identity(monkeypatch, sub):
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("OPERATOR_SUBJECTS", raising=False)
    monkeypatch.delenv("OPERATOR_EMAILS", raising=False)
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {"sub": sub})
    monkeypatch.setattr(agent_api, "audit", type("Audit", (), {
        "emit": staticmethod(lambda *args, **kwargs: None),
    }))


@pytest.fixture
def published(monkeypatch):
    """The published-workflows table configured and stubbed."""
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")
    table = PublishedTable()
    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: table)
    return table


def workflow_item(workflow_id, owner):
    """One live published item, owned by ``owner`` ("" = unstamped)."""
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


def draft_item(workflow_id, drafted_by, *, live=False):
    item = {
        "workflow_id": f"{workflow_id}#draft",
        "draft_of": workflow_id,
        "file": f"{workflow_id}.yaml",
        "workflow": {"id": workflow_id, "enabled": True,
                     "trigger": {"connector": "email", "event": "message.received"},
                     "actions": [{"id": "a1", "type": "webhook",
                                  "url": "https://example.test/hook"}]},
        "enabled": True,
        "base_revision": 1 if live else 0,
        "drafted_by": drafted_by,
        "updated_at": "2026-09-28T00:00:00+00:00",
    }
    return item


def seed(published_table, *items):
    for item in items:
        published_table.put_item(Item=item)


def execution(workflow_id, event_id, started):
    """One completed run's single step, as the executions ledger stores it."""
    return {
        "execution_id": f"{workflow_id}:a1:{event_id}",
        "run_id": f"{workflow_id}:evt-{event_id}",
        "workflow_id": workflow_id,
        "action_id": "a1", "action_type": "webhook", "status": "completed",
        "connector": "email", "event_type": "message.received",
        "started_at": started, "finished_at": started,
        "duration_ms": 5, "expires_at": 9999999999,
    }


# --- helper unit tests -------------------------------------------------------

def test_operator_sees_everything():
    op = visibility.Visibility("anyone", is_operator=True)
    assert op.owner_visible(MINE)
    assert op.owner_visible(THEIRS)
    assert op.owner_visible("")
    assert op.owner_visible(None)
    assert op.workflow_visible("whatever", {})


def test_owner_sees_own_but_not_others():
    viewer = visibility.Visibility(MINE)
    assert viewer.owner_visible(MINE)
    assert not viewer.owner_visible(THEIRS)
    assert viewer.workflow_visible("w1", {"w1": MINE})
    assert not viewer.workflow_visible("w2", {"w2": THEIRS})


def test_missing_owner_is_visible_to_everyone():
    viewer = visibility.Visibility(MINE)
    # No stamp, nothing to backfill: defensive visibility, never hidden data.
    assert viewer.owner_visible("")
    assert viewer.owner_visible(None)
    assert viewer.workflow_visible("old", {})
    # An unknown workflow (deleted, store unreadable) reads as no owner.
    assert viewer.workflow_visible("unknown", {"other": THEIRS})
    # ...but a known, foreign-owned workflow is still hidden.
    assert not viewer.workflow_visible("known", {"known": THEIRS})


def test_for_role_maps_the_role_bands(monkeypatch):
    assert visibility.for_role(MINE, "admin").is_operator
    assert visibility.for_role(MINE, "operator").is_operator
    assert not visibility.for_role(MINE, "editor").is_operator
    assert not visibility.for_role(MINE, "viewer").is_operator
    assert not visibility.for_role(MINE, None).is_operator
    assert not visibility.for_role(MINE, "disabled").is_operator


def test_for_session_scopes_by_the_stored_role(monkeypatch):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": MINE, "role": "viewer"},
                            {"identity": "dev@example.test", "role": "editor"}])
    # Store populated: the allowlisted operator resolves to "operator", the
    # stored viewer row scopes its subject.
    assert visibility.for_session({"sub": "op@example.test", "subject": "subject-9"}).is_operator
    scoped = visibility.for_session({"sub": "user@example.test", "subject": MINE})
    assert not scoped.is_operator
    assert scoped.subject == MINE


def test_owner_of_item_drafts_belong_to_the_drafter(published):
    assert visibility.owner_of_item(workflow_item("w1", THEIRS)) == THEIRS
    assert visibility.owner_of_item(draft_item("w2", MINE)) == MINE
    unstamped = workflow_item("w3", "")
    unstamped.pop("owner")
    assert visibility.owner_of_item(unstamped) == ""  # published_by "" backfills to ""


def test_workflow_owners_maps_live_and_draft_only(published):
    table = published_workflows.get_table()
    seed(table,
         workflow_item("live-mine", MINE),
         draft_item("live-mine", MINE, live=True),      # draft pair: live owns the id
         draft_item("draft-only", THEIRS))
    owners = visibility.workflow_owners()
    assert owners == {"live-mine": MINE, "draft-only": THEIRS}


def test_workflow_owners_unconfigured_is_empty(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    assert visibility.workflow_owners() == {}


def test_workflow_owners_survives_store_failures(monkeypatch):
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")

    class Exploding:
        def scan(self, **_):
            raise RuntimeError("dynamodb down")

    monkeypatch.setattr(published_workflows, "get_table", lambda table_ref=None: Exploding())
    assert visibility.workflow_owners() == {}


# --- designer list -----------------------------------------------------------

def test_designer_list_operator_sees_all_viewer_sees_own(monkeypatch, published):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "viewer"}])
    seed(published,
         workflow_item("mine", MINE),
         workflow_item("theirs", THEIRS),
         workflow_item("unowned", ""),                            # pre-G17, no stamp
         draft_item("draft-only", MINE),
         workflow_item("hidden-live", THEIRS))
    published.items["hidden-live#draft"] = draft_item("hidden-live", MINE, live=True)

    operator = json.loads(admin.route(
        cookie_event(session_token(monkeypatch)), "GET",
        "/api/admin/designer/workflows")["body"])
    assert [row["id"] for row in operator["workflows"]] == [
        "draft-only", "hidden-live", "mine", "theirs", "unowned"]

    viewer = json.loads(admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test",
                                   subject=MINE)), "GET",
        "/api/admin/designer/workflows")["body"])
    # Their workflow and its draft pair disappear; their own draft-only row
    # and everything unowned stay.
    assert [row["id"] for row in viewer["workflows"]] == ["draft-only", "mine", "unowned"]
    assert viewer["workflows"][0]["owner"] == MINE
    assert viewer["workflows"][0]["published"] is False


def test_designer_list_cli_scopes_non_operator(monkeypatch, published):
    # The CLI designer list rides the "workflow.save" gate, so the least
    # role that reaches it is editor — a non-operator owner-scope band.
    roles_env(monkeypatch, [{"identity": "cli-editor", "role": "editor"}])
    agent_identity(monkeypatch, "cli-editor")
    seed(published, workflow_item("mine", "cli-editor"), workflow_item("theirs", "cli-other"))

    listed = json.loads(agent_api.route(
        agent_event("GET", "/api/agent/designer/workflows", "cli-editor"),
        "GET", "/api/agent/designer/workflows")["body"])
    assert [row["id"] for row in listed["workflows"]] == ["mine"]


# --- overview ----------------------------------------------------------------

@pytest.fixture
def overview_env(monkeypatch, published, tmp_path):
    tables = {
        "executions": DictTable(("execution_id",)),
        "connections": DictTable(("connection_id",)),
        "credentials": DictTable(("credential_id",)),
        "api-tokens": DictTable(("token_hash",)),
        "role-assignments": DictTable(("identity",)),
        "task-usage": DictTable(("month", "workflow_id")),
    }
    dynamo(monkeypatch, tables)
    from src.dapier.connections import credentials as credentials_module

    monkeypatch.setattr(credentials_module.boto3, "resource", boto3.resource)
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    monkeypatch.setenv("ROLE_ASSIGNMENTS_TABLE", "role-assignments")
    monkeypatch.setenv("TASK_USAGE_TABLE", "task-usage")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    return tables


def test_overview_operator_sees_all_viewer_sees_own(monkeypatch, overview_env, published):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "viewer"}])
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS),
         workflow_item("unowned", ""))
    for workflow_id in ("mine", "theirs", "unowned"):
        overview_env["task-usage"].put_item(
            Item={"month": "202609", "workflow_id": workflow_id, "tasks": 3})
    overview_env["executions"].put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))
    overview_env["executions"].put_item(Item=execution("theirs", "e2", "2026-09-28T09:00:00+00:00"))

    operator = json.loads(overview_api.overview(cookie_event(
        session_token(monkeypatch)))["body"])
    assert [row["id"] for row in operator["workflows"]] == ["mine", "theirs", "unowned"]
    assert {row["workflow_id"] for row in operator["usage"]} == {"mine", "theirs", "unowned"}
    assert {row["workflow_id"] for row in operator["runs"]} == {"mine", "theirs"}

    viewer = json.loads(overview_api.overview(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject=MINE)),
        visible=visibility.for_role(MINE, "viewer"),
    )["body"])
    assert [row["id"] for row in viewer["workflows"]] == ["mine", "unowned"]
    assert {row["workflow_id"] for row in viewer["usage"]} == {"mine", "unowned"}
    assert [row["workflow_id"] for row in viewer["runs"]] == ["mine"]
    assert [row["workflow_id"] for row in viewer["executions"]] == ["mine"]
    # Aggregates computed after the filter, so the dropdowns never leak ids.
    assert viewer["workflow_tags"] == []


def test_overview_route_filters_for_the_console_viewer(monkeypatch, overview_env, published):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "viewer"}])
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    payload = json.loads(admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject=MINE)),
        "GET", "/api/admin/overview")["body"])
    assert [row["id"] for row in payload["workflows"]] == ["mine"]


# --- runs --------------------------------------------------------------------

@pytest.fixture
def runs_env(monkeypatch):
    table = DictTable(("execution_id",))
    dynamo(monkeypatch, {"executions": table})
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    return table


def test_runs_list_operator_sees_all_viewer_sees_own(monkeypatch, published, runs_env):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "viewer"}])
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    runs_env.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))
    runs_env.put_item(Item=execution("theirs", "e2", "2026-09-28T09:00:00+00:00"))
    # A run whose workflow no longer exists: audit duty keeps it visible.
    runs_env.put_item(Item=execution("gone-wf", "e3", "2026-09-28T08:00:00+00:00"))

    operator = runs_api.api_list(25)
    assert [run["workflow_id"] for run in operator[1]["runs"]] == [
        "mine", "theirs", "gone-wf"]

    viewer = visibility.for_role(MINE, "viewer")
    scoped = runs_api.api_list(25, visible=viewer)
    assert [run["workflow_id"] for run in scoped[1]["runs"]] == ["mine", "gone-wf"]

    exported = runs_api.api_export(visible=viewer)
    rows = [line.split(",")[1] for line in exported[1]["csv"].splitlines()[1:]]
    assert sorted(rows) == ["gone-wf", "mine"]


def test_runs_route_filters_for_the_console_viewer(monkeypatch, published, runs_env):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "viewer"}])
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    runs_env.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))
    runs_env.put_item(Item=execution("theirs", "e2", "2026-09-28T09:00:00+00:00"))
    payload = json.loads(admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject=MINE)),
        "GET", "/api/admin/runs")["body"])
    assert [run["workflow_id"] for run in payload["runs"]] == ["mine"]


def test_runs_cli_scopes_non_operator(monkeypatch, published):
    table = DictTable(("execution_id",))
    dynamo(monkeypatch, {"executions": table})
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    roles_env(monkeypatch, [{"identity": "cli-viewer", "role": "viewer"}])
    agent_identity(monkeypatch, "cli-viewer")
    seed(published, workflow_item("mine", "cli-viewer"), workflow_item("theirs", "cli-other"))
    table.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))
    table.put_item(Item=execution("theirs", "e2", "2026-09-28T09:00:00+00:00"))
    payload = json.loads(agent_api.route(
        agent_event("GET", "/api/agent/runs", "cli-viewer"),
        "GET", "/api/agent/runs")["body"])
    assert [run["workflow_id"] for run in payload["runs"]] == ["mine"]


# --- inbox -------------------------------------------------------------------

@pytest.fixture
def inbox_env(monkeypatch):
    table = DictTable(("inbox_id",))
    dynamo(monkeypatch, {"inbox": table})
    monkeypatch.setenv(inbox_store.TABLE_ENV, "inbox")
    return table


def inbox_row(inbox_id, matched, received_at):
    return {
        "inbox_id": inbox_id,
        "connector": "webhook", "event": "hook", "source": "github",
        "received_at": received_at,
        "status": "matched" if matched else "unmatched",
        "data": {"action": "opened"}, "matched": matched,
    }


def test_inbox_list_operator_sees_all_viewer_sees_own_and_unmatched(monkeypatch, published,
                                                                    inbox_env):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "viewer"}])
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    inbox_env.put_item(Item=inbox_row("evt-1", ["mine"], "2026-09-28T10:01:00+00:00"))
    inbox_env.put_item(Item=inbox_row("evt-2", ["theirs"], "2026-09-28T10:02:00+00:00"))
    inbox_env.put_item(Item=inbox_row("evt-3", [], "2026-09-28T10:03:00+00:00"))
    inbox_env.put_item(Item=inbox_row("evt-4", ["gone-wf"], "2026-09-28T10:04:00+00:00"))

    operator = inbox_store.api_list()
    assert [event["inbox_id"] for event in operator[1]["events"]] == [
        "evt-4", "evt-3", "evt-2", "evt-1"]
    assert operator[1]["total"] == 4

    scoped = inbox_store.api_list(visible=visibility.for_role(MINE, "viewer"))
    assert [event["inbox_id"] for event in scoped[1]["events"]] == ["evt-4", "evt-3", "evt-1"]
    assert scoped[1]["total"] == 3
    # The matched list stays whole on a visible row — it is what ran.
    assert scoped[1]["events"][0]["matched"] == ["gone-wf"]


def test_inbox_route_filters_for_the_console_viewer(monkeypatch, published, inbox_env):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "viewer"}])
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    inbox_env.put_item(Item=inbox_row("evt-1", ["mine"], "2026-09-28T10:01:00+00:00"))
    inbox_env.put_item(Item=inbox_row("evt-2", ["theirs"], "2026-09-28T10:02:00+00:00"))
    payload = json.loads(admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject=MINE)),
        "GET", "/api/admin/triggers/inbox")["body"])
    assert [event["inbox_id"] for event in payload["events"]] == ["evt-1"]


# --- usage -------------------------------------------------------------------

def test_usage_operator_sees_all_viewer_sees_own(monkeypatch, published):
    table = DictTable(("month", "workflow_id"))
    dynamo(monkeypatch, {"task-usage": table})
    monkeypatch.setenv("TASK_USAGE_TABLE", "task-usage")
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    for workflow_id in ("mine", "theirs", "gone-wf"):
        table.put_item(Item={"month": "202609", "workflow_id": workflow_id, "tasks": 3})
    table.put_item(Item={"month": "202609", "workflow_id": usage_rollup.TOTAL_WORKFLOW_ID,
                         "tasks": 9})

    operator = usage_rollup.api_usage(1)
    assert [row["workflow_id"] for row in operator[1]["usage"]] == [
        "theirs", "mine", "gone-wf"]

    scoped = usage_rollup.api_usage(1, visible=visibility.for_role(MINE, "viewer"))
    assert [row["workflow_id"] for row in scoped[1]["usage"]] == ["mine", "gone-wf"]


def test_usage_route_filters_for_the_console_viewer(monkeypatch, published):
    table = DictTable(("month", "workflow_id"))
    dynamo(monkeypatch, {"task-usage": table})
    monkeypatch.setenv("TASK_USAGE_TABLE", "task-usage")
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "viewer"}])
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    for workflow_id in ("mine", "theirs"):
        table.put_item(Item={"month": "202609", "workflow_id": workflow_id, "tasks": 2})
    payload = json.loads(admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject=MINE)),
        "GET", "/api/admin/usage")["body"])
    assert [row["workflow_id"] for row in payload["usage"]] == ["mine"]


# --- G17 Phase 3: the owner-or-operator write gate (helper unit tests) --------

def workflow_dict(workflow_id):
    return {"id": workflow_id, "enabled": True,
            "trigger": {"connector": "email", "event": "message.received"},
            "actions": [{"id": "a1", "type": "webhook",
                         "url": "https://example.test/hook"}]}


def save_body(workflow_id, **extra):
    """The JSON body a save route takes: the workflow YAML, plus extras such
    as renameFrom. Returns the dict — the response helpers encode it."""
    body = {"yaml": yaml.safe_dump(workflow_dict(workflow_id))}
    body.update(extra)
    return body


def test_write_gate_operator_writes_anything(published):
    seed(published, workflow_item("w1", THEIRS))
    assert visibility.ensure_can_write(MINE, "w1", is_operator=True) is None
    assert visibility.ensure_can_write(MINE, "unclaimed", is_operator=True) is None
    assert visibility.ensure_can_write("", "w1", is_operator=True) is None


def test_write_gate_owner_writes_own_nonowner_denied(published):
    seed(published, workflow_item("w1", MINE))
    assert visibility.ensure_can_write(MINE, "w1", is_operator=False) is None
    status, payload = visibility.ensure_can_write(THEIRS, "w1", is_operator=False)
    assert status == 403
    assert "another owner" in payload["error"]
    # The denial never names the owner.
    assert MINE not in payload["error"]


def test_write_gate_ownerless_stored_item_denied_for_nonoperators(published):
    # The deliberate write-side asymmetry: reads stay open on a missing
    # stamp (defensive visibility), writes do not.
    seed(published, workflow_item("pre-g17", ""))
    assert visibility.ensure_can_write(MINE, "pre-g17", is_operator=False)[0] == 403


def test_write_gate_unclaimed_id_is_a_create(published):
    assert visibility.owner_for_write("brand-new") is None
    assert visibility.ensure_can_write(MINE, "brand-new", is_operator=False) is None


def test_write_gate_draft_only_owned_by_drafter(published):
    seed(published, draft_item("d1", MINE))
    assert visibility.ensure_can_write(MINE, "d1", is_operator=False) is None
    assert visibility.ensure_can_write(THEIRS, "d1", is_operator=False)[0] == 403


def test_write_gate_live_pair_answers_to_the_live_owner(published):
    seed(published, workflow_item("w1", THEIRS))
    published.items["w1#draft"] = draft_item("w1", MINE, live=True)
    # Their live workflow with my draft on top: the live owner decides.
    assert visibility.ensure_can_write(MINE, "w1", is_operator=False)[0] == 403


def test_write_gate_store_failures_never_fake_a_denial(monkeypatch):
    monkeypatch.setenv(published_workflows.TABLE_ENV, "published-test")

    class Exploding:
        def get_item(self, Key):
            raise RuntimeError("dynamodb down")

    monkeypatch.setattr(published_workflows, "get_table",
                        lambda table_ref=None: Exploding())
    # The write itself will surface the store problem; the gate must not.
    assert visibility.ensure_can_write(MINE, "w1", is_operator=False) is None


def test_write_gate_unconfigured_store_is_a_create(monkeypatch):
    monkeypatch.delenv(published_workflows.TABLE_ENV, raising=False)
    assert visibility.ensure_can_write(MINE, "w1", is_operator=False) is None


def test_can_write_uses_the_scope(published):
    seed(published, workflow_item("w1", THEIRS))
    assert visibility.for_role(MINE, "admin").can_write("w1") is None
    assert visibility.for_role(MINE, "operator").can_write("w1") is None
    status, _ = visibility.for_role(MINE, "editor").can_write("w1")
    assert status == 403


# --- G17 Phase 3: the write routes (console) ----------------------------------

def console_response(monkeypatch, method, path, body=None,
                     sub="user@example.test", subject=MINE):
    # The console-write test idiom (test_designer.py): the CSRF same-origin
    # check is stubbed; the session cookie carries the role under test.
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    event = cookie_event(session_token(monkeypatch, sub=sub, subject=subject),
                         method=method, path=path)
    if body is not None:
        event["body"] = json.dumps(body)
    return admin.route(event, method, path)


def test_console_toggle_gate(monkeypatch, published):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "editor"}])
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS),
         workflow_item("pre-g17", ""))

    # Owner-editor: own workflow toggles.
    assert console_response(monkeypatch, "PUT",
                            "/api/admin/designer/workflows/mine.yaml",
                            {"enabled": False})["statusCode"] == 200
    # Foreign workflow: denied, untouched.
    denied = console_response(monkeypatch, "PUT",
                              "/api/admin/designer/workflows/theirs.yaml",
                              {"enabled": False})
    assert denied["statusCode"] == 403
    assert "another owner" in json.loads(denied["body"])["error"]
    assert published.items["theirs"]["workflow"]["enabled"] is True
    # Ownerless stored item: denied on writes even though reads show it.
    assert console_response(monkeypatch, "PUT",
                            "/api/admin/designer/workflows/pre-g17.yaml",
                            {"enabled": False})["statusCode"] == 403
    # Operators unchanged: they still write anything.
    assert console_response(monkeypatch, "PUT",
                            "/api/admin/designer/workflows/theirs.yaml",
                            {"enabled": False},
                            sub="op@example.test", subject="subject-op"
                            )["statusCode"] == 200


def test_console_save_gate(monkeypatch, published):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "editor"}])
    seed(published, workflow_item("theirs", THEIRS))

    denied = console_response(monkeypatch, "PUT", "/api/admin/designer/workflows",
                              save_body("theirs"))
    assert denied["statusCode"] == 403
    assert "theirs#draft" not in published.items  # nothing was written
    # A rename cannot smuggle a foreign workflow past the gate either.
    denied = console_response(monkeypatch, "PUT", "/api/admin/designer/workflows",
                              save_body("fresh", renameFrom="theirs.yaml"))
    assert denied["statusCode"] == 403
    # Unclaimed id: the create stays open.
    created = console_response(monkeypatch, "PUT", "/api/admin/designer/workflows",
                               save_body("fresh"))
    assert created["statusCode"] == 200
    assert json.loads(created["body"])["published"] is False


def test_console_bulk_gate_is_per_id(monkeypatch, published):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [{"identity": "user@example.test", "role": "editor"}])
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    payload = json.loads(console_response(
        monkeypatch, "POST", "/api/admin/designer/workflows/bulk",
        {"action": "disable", "ids": ["mine.yaml", "theirs.yaml"]})["body"])
    results = {row["id"]: row for row in payload["results"]}
    assert results["mine.yaml"]["ok"] is True
    assert results["theirs.yaml"]["ok"] is False
    assert "another owner" in results["theirs.yaml"]["error"]
    assert payload["ok"] == 1
    assert published.items["theirs"]["workflow"]["enabled"] is True


# --- G17 Phase 3: the write routes (CLI / agent API) --------------------------

def agent_response(monkeypatch, method, path, body=None, sub="cli-owner"):
    event = agent_event(method, path, sub)
    if body is not None:
        event["body"] = json.dumps(body)
    return agent_api.route(event, method, path)


def cli_editor(monkeypatch, published, *items):
    roles_env(monkeypatch, [{"identity": "cli-owner", "role": "editor"}])
    agent_identity(monkeypatch, "cli-owner")
    seed(published, *items)


def test_cli_save_nonowner_denied_create_allowed(monkeypatch, published):
    cli_editor(monkeypatch, published,
               workflow_item("mine", "cli-owner"), workflow_item("theirs", "cli-other"))

    created = agent_response(monkeypatch, "PUT", "/api/agent/designer/workflows",
                             save_body("fresh"))
    assert created["statusCode"] == 200
    assert json.loads(created["body"])["published"] is False
    own = agent_response(monkeypatch, "PUT", "/api/agent/designer/workflows",
                         save_body("mine"))
    assert own["statusCode"] == 200
    denied = agent_response(monkeypatch, "PUT", "/api/agent/designer/workflows",
                            save_body("theirs"))
    assert denied["statusCode"] == 403
    assert "theirs#draft" not in published.items


def test_cli_source_write_routes_gate_nonowners(monkeypatch, published):
    cli_editor(monkeypatch, published,
               workflow_item("mine", "cli-owner"), workflow_item("theirs", "cli-other"))
    executions = DictTable(("execution_id",))
    dynamo(monkeypatch, {"executions": executions})
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    gated = [
        ("PUT", "/api/agent/designer/workflows/theirs.yaml", {"enabled": False}),
        ("DELETE", "/api/agent/designer/workflows/theirs.yaml", None),
        ("PUT", "/api/agent/designer/workflows/theirs.yaml/tags", {"tags": ["x"]}),
        ("PUT", "/api/agent/designer/workflows/theirs.yaml/folder", {"folder": "x"}),
        ("POST", "/api/agent/designer/workflows/theirs.yaml/publish", None),
        ("DELETE", "/api/agent/designer/workflows/theirs.yaml/draft", None),
        ("POST", "/api/agent/designer/workflows/theirs.yaml/rollback", {"revision": 1}),
        ("POST", "/api/agent/designer/workflows/theirs.yaml/test", {"event": {}}),
        ("POST", "/api/agent/designer/workflows/theirs.yaml/test-step",
         {"action_id": "a1", "event": {}}),
        # (the template-flag PUT is operator-banded — roles.py's default —
        # so a non-operator is denied by the role gate before ownership)
        ("POST", "/api/agent/storage/theirs", {"key": "k", "value": "v"}),
        ("DELETE", "/api/agent/storage/theirs", None),
    ]
    for method, path, body in gated:
        response = agent_response(monkeypatch, method, path, body)
        assert response["statusCode"] == 403, (method, path, response["body"])


def test_cli_owner_writes_own_workflows(monkeypatch, published):
    cli_editor(monkeypatch, published, workflow_item("mine", "cli-owner"))
    published.items["mine#draft"] = draft_item("mine", "cli-owner", live=True)

    assert agent_response(monkeypatch, "DELETE",
                          "/api/agent/designer/workflows/mine.yaml/draft"
                          )["statusCode"] == 200
    assert agent_response(monkeypatch, "PUT",
                          "/api/agent/designer/workflows/mine.yaml",
                          {"enabled": False})["statusCode"] == 200
    # The toggle above is now revision 2; a draft based on it publishes.
    published.items["mine#draft"] = draft_item("mine", "cli-owner", live=True)
    published.items["mine#draft"]["base_revision"] = 2
    published_ok = agent_response(monkeypatch, "POST",
                                  "/api/agent/designer/workflows/mine.yaml/publish")
    assert published_ok["statusCode"] == 200
    assert json.loads(published_ok["body"])["published"] is True


def test_cli_bulk_gate_is_per_id(monkeypatch, published):
    cli_editor(monkeypatch, published,
               workflow_item("mine", "cli-owner"), workflow_item("theirs", "cli-other"))
    payload = json.loads(agent_response(
        monkeypatch, "POST", "/api/agent/designer/workflows/bulk",
        {"action": "disable", "ids": ["mine.yaml", "theirs.yaml"]})["body"])
    results = {row["id"]: row for row in payload["results"]}
    assert results["mine.yaml"]["ok"] is True
    assert results["theirs.yaml"]["ok"] is False
    assert "another owner" in results["theirs.yaml"]["error"]
    assert published.items["theirs"]["workflow"]["enabled"] is True


def test_cli_keyed_test_gated_inline_test_not(monkeypatch, published):
    cli_editor(monkeypatch, published, workflow_item("theirs", "cli-other"))
    # Keyed by a saved workflow: the gate applies.
    keyed = agent_response(monkeypatch, "POST",
                           "/api/agent/designer/workflows/theirs.yaml/test",
                           {"event": {}})
    assert keyed["statusCode"] == 403
    # Inline (the designer's unsaved draft): not keyed by a stored row.
    inline = agent_response(monkeypatch, "POST",
                            "/api/agent/designer/workflows/test-step",
                            {"action_id": "a1", "event": {},
                             "workflow": workflow_dict("theirs")})
    assert inline["statusCode"] != 403


def test_cli_duplicate_is_a_create_not_an_ownership_check(monkeypatch, published):
    # Duplicating forks into a NEW id (the domain 409s on a collision), so
    # the write gate's create rule applies: nothing to deny for a non-owner.
    cli_editor(monkeypatch, published, workflow_item("mine", "cli-owner"))
    copied = agent_response(monkeypatch, "POST",
                            "/api/agent/designer/workflows/mine.yaml/duplicate", {})
    assert copied["statusCode"] == 200
    assert json.loads(copied["body"])["file"] == "mine-copy.yaml"


def test_cli_template_apply_creates_under_its_operator_band(monkeypatch, published):
    # Template apply is operator-banded on both surfaces (roles.py's
    # default), and its write target is a new id — the fork — so no
    # ownership check applies; this proves the route still creates.
    template = workflow_item("tpl", "cli-other")
    template["workflow"]["template"] = True
    roles_env(monkeypatch, [{"identity": "cli-op", "role": "operator"}])
    agent_identity(monkeypatch, "cli-op")
    seed(published, template)
    applied = agent_response(monkeypatch, "POST",
                             "/api/agent/designer/templates/tpl.yaml/apply", {},
                             sub="cli-op")
    assert applied["statusCode"] == 200
    assert json.loads(applied["body"])["file"] == "tpl-copy.yaml"


def test_cli_storage_gate(monkeypatch, published):
    cli_editor(monkeypatch, published, workflow_item("theirs", "cli-other"))
    dynamo(monkeypatch, {"workflow-state": DictTable(("scope", "key"))})
    monkeypatch.setenv("STORAGE_TABLE", "workflow-state")

    denied = agent_response(monkeypatch, "POST", "/api/agent/storage/theirs",
                            {"key": "k", "value": "v"})
    assert denied["statusCode"] == 403
    # The owner may write their workflow's storage.
    roles_env(monkeypatch, [{"identity": "cli-owner", "role": "editor"},
                            {"identity": "cli-other", "role": "editor"}])
    agent_identity(monkeypatch, "cli-other")
    allowed = agent_response(monkeypatch, "POST", "/api/agent/storage/theirs",
                             {"key": "k", "value": "v"}, sub="cli-other")
    assert allowed["statusCode"] == 200
    assert json.loads(allowed["body"])["workflow"] == "theirs"
