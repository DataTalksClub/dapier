"""G17 Phase 2: the visibility helper (auth/visibility.py) and the read
surfaces it filters — designer list, overview, runs, inbox, usage.

Rule under test: operators see everything; a subject sees the workflows it
owns (draft-only rows by drafted_by); anything with no owner — including a
row whose workflow no longer exists — stays visible to everyone.

The gates let only operators through (the operator allowlist is the whole
auth story since the roles store left), so the scoped reads and the
owner-or-operator WRITE gate (G17 Phase 3, ensure_can_write) are exercised
at the domain level with explicit scopes; at the routes, a non-allowlisted
identity is denied before any of it applies.
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


def test_for_session_scopes_by_the_operator_allowlist(monkeypatch):
    operator_env(monkeypatch)
    # On the allowlist: an operator, everything visible.
    assert visibility.for_session(
        {"sub": "op@example.test", "subject": "subject-9"}).is_operator
    # Off it: a subject-scoped view, everything filtered against its owner.
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

def test_designer_list_operator_sees_all(monkeypatch, published):
    operator_env(monkeypatch)
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


def test_designer_list_denies_non_operators(monkeypatch, published):
    operator_env(monkeypatch)
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    response = admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test",
                                   subject=MINE)), "GET",
        "/api/admin/designer/workflows")
    assert response["statusCode"] == 403
    assert json.loads(response["body"])["error"] == "Operator authorization required"


def test_designer_list_cli_operator_sees_all(monkeypatch, published):
    agent_identity(monkeypatch, "cli-op")
    seed(published, workflow_item("mine", "cli-op"), workflow_item("theirs", "cli-other"))

    listed = json.loads(agent_api.route(
        agent_event("GET", "/api/agent/designer/workflows", "cli-op"),
        "GET", "/api/agent/designer/workflows")["body"])
    assert [row["id"] for row in listed["workflows"]] == ["mine", "theirs"]


def test_designer_list_cli_denies_non_operators(monkeypatch, published):
    # No email claim and an allowlist that names someone else: the gate
    # denies before any ownership question arises.
    agent_identity(monkeypatch, "cli-nobody")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@example.test")
    seed(published, workflow_item("mine", "cli-nobody"))

    response = agent_api.route(
        agent_event("GET", "/api/agent/designer/workflows", "cli-nobody"),
        "GET", "/api/agent/designer/workflows")
    assert response["statusCode"] == 403


# --- overview ----------------------------------------------------------------

@pytest.fixture
def overview_env(monkeypatch, published, tmp_path):
    tables = {
        "executions": DictTable(("execution_id",)),
        "connections": DictTable(("connection_id",)),
        "credentials": DictTable(("credential_id",)),
        "api-tokens": DictTable(("token_hash",)),
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
    monkeypatch.setenv("TASK_USAGE_TABLE", "task-usage")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    return tables


def test_overview_operator_sees_all_and_scopes_apply(monkeypatch, overview_env, published):
    operator_env(monkeypatch)
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

    # The scoped variant (the domain-level contract the scope threading
    # relies on): a subject-scoped view sees its own and the unowned rows.
    scoped = json.loads(overview_api.overview(
        cookie_event(session_token(monkeypatch)),
        visible=visibility.Visibility(MINE),
    )["body"])
    assert [row["id"] for row in scoped["workflows"]] == ["mine", "unowned"]
    assert {row["workflow_id"] for row in scoped["usage"]} == {"mine", "unowned"}
    assert [row["workflow_id"] for row in scoped["runs"]] == ["mine"]
    assert [row["workflow_id"] for row in scoped["executions"]] == ["mine"]
    # Aggregates computed after the filter, so the dropdowns never leak ids.
    assert scoped["workflow_tags"] == []


def test_overview_route_denies_non_operators(monkeypatch, overview_env, published):
    operator_env(monkeypatch)
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)
    payload = admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject=MINE)),
        "GET", "/api/admin/overview")
    assert payload["statusCode"] == 403


# --- runs --------------------------------------------------------------------

@pytest.fixture
def runs_env(monkeypatch):
    table = DictTable(("execution_id",))
    dynamo(monkeypatch, {"executions": table})
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    return table


def test_runs_list_operator_sees_all_and_scopes_apply(monkeypatch, published, runs_env):
    operator_env(monkeypatch)
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    runs_env.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))
    runs_env.put_item(Item=execution("theirs", "e2", "2026-09-28T09:00:00+00:00"))
    # A run whose workflow no longer exists: audit duty keeps it visible.
    runs_env.put_item(Item=execution("gone-wf", "e3", "2026-09-28T08:00:00+00:00"))

    operator = runs_api.api_list(25)
    assert [run["workflow_id"] for run in operator[1]["runs"]] == [
        "mine", "theirs", "gone-wf"]

    scoped = runs_api.api_list(25, visible=visibility.Visibility(MINE))
    assert [run["workflow_id"] for run in scoped[1]["runs"]] == ["mine", "gone-wf"]

    exported = runs_api.api_export(visible=visibility.Visibility(MINE))
    rows = [line.split(",")[1] for line in exported[1]["csv"].splitlines()[1:]]
    assert sorted(rows) == ["gone-wf", "mine"]


def test_runs_route_denies_non_operators(monkeypatch, published, runs_env):
    operator_env(monkeypatch)
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    runs_env.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))
    runs_env.put_item(Item=execution("theirs", "e2", "2026-09-28T09:00:00+00:00"))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)
    payload = admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject=MINE)),
        "GET", "/api/admin/runs")
    assert payload["statusCode"] == 403


def test_runs_cli_denies_non_operators(monkeypatch, published):
    table = DictTable(("execution_id",))
    dynamo(monkeypatch, {"executions": table})
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    agent_identity(monkeypatch, "cli-nobody")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@example.test")
    seed(published, workflow_item("mine", "cli-nobody"), workflow_item("theirs", "cli-other"))
    table.put_item(Item=execution("mine", "e1", "2026-09-28T10:00:00+00:00"))
    table.put_item(Item=execution("theirs", "e2", "2026-09-28T09:00:00+00:00"))
    payload = agent_api.route(
        agent_event("GET", "/api/agent/runs", "cli-nobody"),
        "GET", "/api/agent/runs")
    assert payload["statusCode"] == 403


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


def test_inbox_list_operator_sees_all_and_scopes_apply(monkeypatch, published, inbox_env):
    operator_env(monkeypatch)
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    inbox_env.put_item(Item=inbox_row("evt-1", ["mine"], "2026-09-28T10:01:00+00:00"))
    inbox_env.put_item(Item=inbox_row("evt-2", ["theirs"], "2026-09-28T10:02:00+00:00"))
    inbox_env.put_item(Item=inbox_row("evt-3", [], "2026-09-28T10:03:00+00:00"))
    inbox_env.put_item(Item=inbox_row("evt-4", ["gone-wf"], "2026-09-28T10:04:00+00:00"))

    operator = inbox_store.api_list()
    assert [event["inbox_id"] for event in operator[1]["events"]] == [
        "evt-4", "evt-3", "evt-2", "evt-1"]
    assert operator[1]["total"] == 4

    scoped = inbox_store.api_list(visible=visibility.Visibility(MINE))
    assert [event["inbox_id"] for event in scoped[1]["events"]] == ["evt-4", "evt-3", "evt-1"]
    assert scoped[1]["total"] == 3
    # The matched list stays whole on a visible row — it is what ran.
    assert scoped[1]["events"][0]["matched"] == ["gone-wf"]


def test_inbox_route_denies_non_operators(monkeypatch, published, inbox_env):
    operator_env(monkeypatch)
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    inbox_env.put_item(Item=inbox_row("evt-1", ["mine"], "2026-09-28T10:01:00+00:00"))
    inbox_env.put_item(Item=inbox_row("evt-2", ["theirs"], "2026-09-28T10:02:00+00:00"))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)
    payload = admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject=MINE)),
        "GET", "/api/admin/triggers/inbox")
    assert payload["statusCode"] == 403


# --- usage -------------------------------------------------------------------

def test_usage_operator_sees_all_and_scopes_apply(monkeypatch, published):
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

    scoped = usage_rollup.api_usage(1, visible=visibility.Visibility(MINE))
    assert [row["workflow_id"] for row in scoped[1]["usage"]] == ["mine", "gone-wf"]


def test_usage_route_denies_non_operators(monkeypatch, published):
    table = DictTable(("month", "workflow_id"))
    dynamo(monkeypatch, {"task-usage": table})
    monkeypatch.setenv("TASK_USAGE_TABLE", "task-usage")
    operator_env(monkeypatch)
    seed(published, workflow_item("mine", MINE), workflow_item("theirs", THEIRS))
    for workflow_id in ("mine", "theirs"):
        table.put_item(Item={"month": "202609", "workflow_id": workflow_id, "tasks": 2})
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)
    payload = admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject=MINE)),
        "GET", "/api/admin/usage")
    assert payload["statusCode"] == 403


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
    assert visibility.Visibility(MINE, is_operator=True).can_write("w1") is None
    status, _ = visibility.Visibility(MINE).can_write("w1")
    assert status == 403


# --- G17 Phase 3: the write routes --------------------------------------------

def console_response(monkeypatch, method, path, body=None,
                     sub="op@example.test", subject=MINE):
    # The console-write test idiom (test_designer.py): the CSRF same-origin
    # check is stubbed; the session cookie carries the identity under test.
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    event = cookie_event(session_token(monkeypatch, sub=sub, subject=subject),
                         method=method, path=path)
    if body is not None:
        event["body"] = json.dumps(body)
    return admin.route(event, method, path)


def test_console_write_routes_write_for_operators(monkeypatch, published):
    operator_env(monkeypatch)
    seed(published, workflow_item("theirs", THEIRS))
    assert console_response(monkeypatch, "PUT",
                            "/api/admin/designer/workflows/theirs.yaml",
                            {"enabled": False})["statusCode"] == 200
    created = console_response(monkeypatch, "PUT", "/api/admin/designer/workflows",
                               save_body("fresh"))
    assert created["statusCode"] == 200


def test_console_write_routes_deny_non_operators(monkeypatch, published):
    operator_env(monkeypatch)
    seed(published, workflow_item("theirs", THEIRS))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: None)

    # Off the allowlist, the gate denies before any ownership question.
    for method, path, body in [
        ("PUT", "/api/admin/designer/workflows/theirs.yaml", {"enabled": False}),
        ("PUT", "/api/admin/designer/workflows", save_body("fresh")),
        ("POST", "/api/admin/designer/workflows/bulk",
         {"action": "disable", "ids": ["theirs.yaml"]}),
    ]:
        response = console_response(monkeypatch, method, path, body,
                                    sub="user@example.test")
        assert response["statusCode"] == 403, (method, path)
    assert published.items["theirs"]["workflow"]["enabled"] is True
    assert "fresh#draft" not in published.items  # nothing was written


# --- the CLI write routes ------------------------------------------------------

def agent_response(monkeypatch, method, path, body=None, sub="cli-op"):
    event = agent_event(method, path, sub)
    if body is not None:
        event["body"] = json.dumps(body)
    return agent_api.route(event, method, path)


def test_cli_write_routes_write_for_operators(monkeypatch, published):
    agent_identity(monkeypatch, "cli-op")
    seed(published, workflow_item("mine", "cli-op"))

    created = agent_response(monkeypatch, "PUT", "/api/agent/designer/workflows",
                             save_body("fresh"))
    assert created["statusCode"] == 200
    assert json.loads(created["body"])["published"] is False
    own = agent_response(monkeypatch, "PUT", "/api/agent/designer/workflows",
                         save_body("mine"))
    assert own["statusCode"] == 200


def test_cli_write_routes_deny_non_operators(monkeypatch, published):
    agent_identity(monkeypatch, "cli-nobody")
    monkeypatch.setenv("OPERATOR_EMAILS", "op@example.test")
    seed(published, workflow_item("theirs", "cli-other"))
    executions = DictTable(("execution_id",))
    dynamo(monkeypatch, {"executions": executions})
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")

    gated = [
        ("PUT", "/api/agent/designer/workflows", save_body("fresh")),
        ("PUT", "/api/agent/designer/workflows/theirs.yaml", {"enabled": False}),
        ("DELETE", "/api/agent/designer/workflows/theirs.yaml", None),
        ("PUT", "/api/agent/designer/workflows/theirs.yaml/tags", {"tags": ["x"]}),
        ("POST", "/api/agent/designer/workflows/theirs.yaml/publish", None),
        ("POST", "/api/agent/designer/workflows/theirs.yaml/test", {"event": {}}),
        # Even the inline (unsaved-draft) test rides the operator gate.
        ("POST", "/api/agent/designer/workflows/test-step",
         {"action_id": "a1", "event": {}, "workflow": workflow_dict("theirs")}),
        ("POST", "/api/agent/storage/theirs", {"key": "k", "value": "v"}),
    ]
    for method, path, body in gated:
        response = agent_response(monkeypatch, method, path, body)
        assert response["statusCode"] == 403, (method, path, response["body"])
