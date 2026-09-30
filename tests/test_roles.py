"""Roles v1: stored user roles (viewer/editor/operator/admin) over the
operator gate, with user management as its own admin band.

Covers the domain module (auth/roles.py), the console dispatcher's per-route
bands, /api/admin/me's role report, the users surfaces (/api/admin/users via
users_routes and /api/agent/users via agent.users_api), and the CLI's
`dapier users` commands.
"""
import json
import time

import boto3
import pytest

from src.dapier.api import admin, agent as agent_api
from src.dapier.api.admin import users_routes
from src.dapier.auth import roles, session


def session_token(monkeypatch, sub="op@example.test", subject="subject-1"):
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    return session._sign({"sub": sub, "subject": subject, "exp": int(time.time()) + 3600})


def cookie_event(token, method="GET", path="/api/admin/overview", headers=None, body=None):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test", **(headers or {})},
        "cookies": [f"dapier_session={token}"],
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


ORIGIN = {"origin": "https://dapier.example.test"}


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


def dynamo(monkeypatch, tables):
    class Dynamo:
        def Table(self, name):
            return tables[name]

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return tables


def operator_env(monkeypatch, emails="op@example.test", subjects=""):
    monkeypatch.setenv("OPERATOR_EMAILS", emails)
    monkeypatch.setenv("OPERATOR_SUBJECTS", subjects)


def roles_env(monkeypatch, rows, audit=False):
    tables = {"role-assignments": DictTable(("identity",))}
    for item in rows:
        tables["role-assignments"].put_item(Item=item)
    if audit:
        tables["audit"] = DictTable(("audit_id",))
        monkeypatch.setenv("AUDIT_TABLE", "audit")
    else:
        monkeypatch.delenv("AUDIT_TABLE", raising=False)
    dynamo(monkeypatch, tables)
    monkeypatch.setenv("ROLE_ASSIGNMENTS_TABLE", "role-assignments")
    return tables["role-assignments"]


def overview_env(monkeypatch, tmp_path):
    tables = {
        "executions": DictTable(("execution_id",)),
        "connections": DictTable(("connection_id",)),
        "credentials": DictTable(("credential_id",)),
        "api-tokens": DictTable(("token_hash",)),
        "role-assignments": DictTable(("identity",)),
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
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    return tables["role-assignments"]


# --- domain -----------------------------------------------------------------

def test_normalize_identity_and_validate_role():
    assert roles.normalize_identity(" User@Example.Test ") == "user@example.test"
    assert roles.normalize_identity("subject-7") == "subject-7"
    with pytest.raises(ValueError):
        roles.normalize_identity("   ")
    assert roles.validate_role(" EDITOR ") == "editor"
    with pytest.raises(ValueError):
        roles.validate_role("owner")


def test_satisfies_follows_the_hierarchy():
    assert roles.satisfies("viewer", "viewer") is True
    assert roles.satisfies("editor", "viewer") is True
    assert roles.satisfies("operator", "editor") is True
    assert roles.satisfies("admin", "operator") is True
    assert roles.satisfies("viewer", "editor") is False
    assert roles.satisfies("operator", "admin") is False
    assert roles.satisfies(None, "viewer") is False
    assert roles.satisfies("disabled", "viewer") is False


def test_stored_assignment_wins_over_the_allowlist(monkeypatch):
    operator_env(monkeypatch)
    table = roles_env(monkeypatch, [
        {"identity": "subject-1", "role": "viewer"},            # narrows an operator
        {"identity": "subject-7", "role": "editor"},            # widens a non-operator
        {"identity": "user@example.test", "role": "editor",
         "disabled": True},                                     # denies everywhere
    ])
    assert roles.effective_role({"sub": "op@example.test", "subject": "subject-1"}, table) == "viewer"
    assert roles.effective_role({"sub": "user@example.test", "subject": "subject-7"}, table) == "editor"
    assert roles.effective_role({"sub": "user@example.test", "subject": "subject-9"}, table) == "disabled"


def test_allowlist_fallback_admin_while_empty_operator_once_populated(monkeypatch):
    operator_env(monkeypatch)
    table = roles_env(monkeypatch, [])
    payload = {"sub": "op@example.test", "subject": "subject-1"}
    assert roles.effective_role(payload, table) == "admin"
    table.put_item(Item={"identity": "subject-7", "role": "viewer"})
    assert roles.effective_role(payload, table) == "operator"
    assert roles.effective_role({"sub": "x@example.test", "subject": "subject-8"}, table) is None


def test_effective_role_survives_table_problems(monkeypatch):
    operator_env(monkeypatch)
    payload = {"sub": "op@example.test", "subject": "subject-1"}

    class Exploding:
        def get_item(self, **kwargs):
            raise RuntimeError("no table")

        def scan(self, **kwargs):
            raise RuntimeError("no table")

    # Store unreadable: the allowlist verdict stands (admin, legacy semantics).
    assert roles.effective_role(payload, Exploding()) == "admin"
    assert roles.effective_role({"sub": "x@example.test", "subject": "s"}, Exploding()) is None
    monkeypatch.delenv("ROLE_ASSIGNMENTS_TABLE", raising=False)
    assert roles.effective_role(payload) == "admin"


def test_minimum_for_route_bands():
    assert roles.minimum_for_route("GET", "/api/admin/runs") == "viewer"
    assert roles.minimum_for_route("GET", "/api/admin/designer/workflows/foo.yaml/versions") == "viewer"
    assert roles.minimum_for_route("PUT", "/api/admin/designer/workflows") == "editor"
    assert roles.minimum_for_route("DELETE", "/api/admin/designer/workflows/foo.yaml") == "editor"
    assert roles.minimum_for_route("POST", "/api/admin/designer/workflows/test-step") == "editor"
    assert roles.minimum_for_route("GET", "/api/admin/users") == "admin"
    assert roles.minimum_for_route("DELETE", "/api/admin/users/user@example.test") == "admin"
    assert roles.minimum_for_route("PUT", "/api/admin/grants") == "operator"
    assert roles.minimum_for_route("PUT", "/api/admin/credentials/slack") == "operator"
    assert roles.minimum_for_route("GET", "/api/admin/no-such-route") == "operator"


def test_minimum_for_action_bands():
    assert roles.minimum_for_action("runs") == "viewer"
    assert roles.minimum_for_action("runs.replay") == "editor"
    assert roles.minimum_for_action("runs.cancel") == "editor"
    assert roles.minimum_for_action("runs.replay-failed") == "editor"
    assert roles.minimum_for_action("triggers.inbox-replay") == "editor"
    assert roles.minimum_for_action("errors.send-digest") == "editor"
    assert roles.minimum_for_action("workflow.save") == "editor"
    assert roles.minimum_for_action("users") == "admin"
    assert roles.minimum_for_action("grant") == "operator"
    assert roles.minimum_for_action("anything-unknown") == "operator"


def test_set_role_persists_and_audits(monkeypatch):
    operator_env(monkeypatch)
    tables = {"role-assignments": DictTable(("identity",)), "audit": DictTable(("audit_id",))}
    dynamo(monkeypatch, tables)
    monkeypatch.setenv("ROLE_ASSIGNMENTS_TABLE", "role-assignments")
    monkeypatch.setenv("AUDIT_TABLE", "audit")
    status, user = roles.api_set_role(
        {"subject": "Dev@Example.test", "role": "editor", "display_name": "Dev"},
        operator="op@example.test")
    assert status == 200
    assert user == {"subject": "dev@example.test", "role": "editor",
                    "display_name": "Dev", "assigned_by": "op@example.test",
                    "assigned_at": user["assigned_at"], "updated_at": user["updated_at"],
                    "updated_by": "op@example.test"}
    stored = tables["role-assignments"].get_item(Key={"identity": "dev@example.test"})["Item"]
    assert stored["role"] == "editor"
    audits = list(tables["audit"].items.values())
    assert [(row["action"], row["outcome"], row["actor_subject"]) for row in audits] == [
        ("users.set-role", "editor", "op@example.test")]
    bad = roles.api_set_role({"subject": "x", "role": "owner"}, operator="op")
    assert bad == (400, {"error": "Role must be one of: viewer, editor, operator, admin"})


def test_last_admin_guard(monkeypatch):
    operator_env(monkeypatch)
    table = roles_env(monkeypatch, [
        {"identity": "sole-admin@example.test", "role": "admin"},
    ])
    demote = roles.api_set_role({"subject": "sole-admin@example.test", "role": "viewer"},
                                operator="op@example.test")
    assert demote[0] == 409
    remove = roles.api_remove_role("sole-admin@example.test", operator="op@example.test")
    assert remove[0] == 409
    disable = roles.api_set_role({"subject": "sole-admin@example.test", "role": "admin",
                                  "disabled": True}, operator="op@example.test")
    assert disable[0] == 409
    # A second admin makes every change legal.
    roles.api_set_role({"subject": "second@example.test", "role": "admin"},
                       operator="op@example.test")
    assert roles.api_set_role({"subject": "sole-admin@example.test", "role": "viewer"},
                              operator="second@example.test")[0] == 200


def test_remove_role_roundtrip(monkeypatch):
    operator_env(monkeypatch)
    table = roles_env(monkeypatch, [
        {"identity": "dev@example.test", "role": "editor"},
    ])
    missing = roles.api_remove_role("nobody@example.test", operator="op")
    assert missing[0] == 404
    ok = roles.api_remove_role("dev@example.test", operator="op@example.test")
    assert ok[0] == 200
    assert table.scan()["Items"] == []
    unconfigured = roles.api_set_role({"subject": "x", "role": "viewer"}, operator="op")
    assert unconfigured[0] != 503  # table is configured here
    monkeypatch.delenv("ROLE_ASSIGNMENTS_TABLE", raising=False)
    assert roles.api_set_role({"subject": "x", "role": "viewer"}, operator="op")[0] == 503
    assert roles.api_remove_role("x", operator="op")[0] == 503


# --- console dispatcher -----------------------------------------------------

def test_me_reports_the_effective_role(monkeypatch):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [
        {"identity": "user@example.test", "role": "editor"},
    ])
    operator_token = session_token(monkeypatch)
    editor_token = session_token(monkeypatch, sub="user@example.test", subject="subject-7")
    operator_me = json.loads(admin.route(cookie_event(operator_token), "GET", "/api/admin/me")["body"])
    editor_me = json.loads(admin.route(cookie_event(editor_token), "GET", "/api/admin/me")["body"])
    # The store is populated, so the allowlist operator resolves to "operator";
    # a non-allowlisted session with a stored row resolves to that row.
    assert operator_me == {"username": "op@example.test", "operator": True, "role": "operator"}
    assert editor_me == {"username": "user@example.test", "operator": False, "role": "editor"}


def test_viewer_reads_and_roleless_is_still_denied(monkeypatch, tmp_path):
    operator_env(monkeypatch)
    table = overview_env(monkeypatch, tmp_path)
    table.put_item(Item={"identity": "user@example.test", "role": "viewer"})
    viewer = session_token(monkeypatch, sub="user@example.test", subject="subject-7")
    roleless = session_token(monkeypatch, sub="stranger@example.test", subject="subject-8")
    assert admin.route(cookie_event(viewer), "GET", "/api/admin/overview")["statusCode"] == 200
    denied = admin.route(cookie_event(roleless), "GET", "/api/admin/overview")
    assert denied["statusCode"] == 403
    assert json.loads(denied["body"])["error"] == "Operator authorization required"


def test_viewer_cannot_edit_but_editor_can(monkeypatch, tmp_path):
    operator_env(monkeypatch)
    table = overview_env(monkeypatch, tmp_path)
    table.put_item(Item={"identity": "user@example.test", "role": "editor"})
    table.put_item(Item={"identity": "peeker@example.test", "role": "viewer"})
    editor = session_token(monkeypatch, sub="user@example.test", subject="subject-7")
    viewer = session_token(monkeypatch, sub="peeker@example.test", subject="subject-8")
    denied = admin.route(
        cookie_event(viewer, method="PUT", path="/api/admin/designer/workflows",
                     headers=ORIGIN, body={}),
        "PUT", "/api/admin/designer/workflows")
    assert denied["statusCode"] == 403
    assert json.loads(denied["body"])["error"] == "This action needs the 'editor' role"
    # The editor clears the role gate: the save itself answers (400 for an
    # empty body), proving the denial came from roles, not the gate.
    passed = admin.route(
        cookie_event(editor, method="PUT", path="/api/admin/designer/workflows",
                     headers=ORIGIN, body={}),
        "PUT", "/api/admin/designer/workflows")
    assert passed["statusCode"] != 403


def test_editor_cannot_manage_connections(monkeypatch, tmp_path):
    operator_env(monkeypatch)
    table = overview_env(monkeypatch, tmp_path)
    table.put_item(Item={"identity": "user@example.test", "role": "editor"})
    editor = session_token(monkeypatch, sub="user@example.test", subject="subject-7")
    credential = admin.route(
        cookie_event(editor, method="PUT", path="/api/admin/credentials/slack",
                     headers=ORIGIN, body={"token": "x"}),
        "PUT", "/api/admin/credentials/slack")
    assert credential["statusCode"] == 403
    assert json.loads(credential["body"])["error"] == "This action needs the 'operator' role"


def test_users_surface_is_the_admin_band(monkeypatch, tmp_path):
    operator_env(monkeypatch)
    table = overview_env(monkeypatch, tmp_path)
    # Empty store: the allowlist operator is the bootstrap admin.
    operator = session_token(monkeypatch)
    first = admin.route(
        cookie_event(operator, method="POST", path="/api/admin/users",
                     headers=ORIGIN, body={"subject": "Admin@Example.test", "role": "admin"}),
        "POST", "/api/admin/users")
    assert first["statusCode"] == 200
    # The store is populated: the allowlist operator is now only an operator.
    second = admin.route(
        cookie_event(operator, method="POST", path="/api/admin/users",
                     headers=ORIGIN, body={"subject": "x@example.test", "role": "viewer"}),
        "POST", "/api/admin/users")
    assert second["statusCode"] == 403
    assert json.loads(second["body"])["error"] == "This action needs the 'admin' role"
    # The stored admin manages users; an editor is refused.
    admin_token = session_token(monkeypatch, sub="admin@example.test", subject="subject-2")
    listed = admin.route(cookie_event(admin_token), "GET", "/api/admin/users")
    assert listed["statusCode"] == 200
    users = json.loads(listed["body"])["users"]
    assert [user["role"] for user in users] == ["admin"]
    viewer = admin.route(
        cookie_event(session_token(monkeypatch, sub="user@example.test", subject="subject-7"),
                     method="POST", path="/api/admin/users", headers=ORIGIN,
                     body={"subject": "y@example.test", "role": "viewer"}),
        "POST", "/api/admin/users")
    assert viewer["statusCode"] == 403


def test_users_crud_from_the_console(monkeypatch):
    operator_env(monkeypatch)
    table = roles_env(monkeypatch, [], audit=True)
    bootstrap = session_token(monkeypatch)
    # Bootstrap: the store is empty, so the allowlist operator may create the
    # first admin; from then on only stored admins manage users.
    first = admin.route(
        cookie_event(bootstrap, method="POST", path="/api/admin/users",
                     headers=ORIGIN, body={"subject": "Admin@Example.test", "role": "admin"}),
        "POST", "/api/admin/users")
    assert first["statusCode"] == 200
    operator_now = admin.route(
        cookie_event(bootstrap, method="POST", path="/api/admin/users",
                     headers=ORIGIN, body={"subject": "x@example.test", "role": "viewer"}),
        "POST", "/api/admin/users")
    assert operator_now["statusCode"] == 403
    steward = session_token(monkeypatch, sub="admin@example.test", subject="subject-2")
    set_response = admin.route(
        cookie_event(steward, method="POST", path="/api/admin/users",
                     headers=ORIGIN, body={"subject": "Dev@Example.test", "role": "viewer"}),
        "POST", "/api/admin/users")
    assert set_response["statusCode"] == 200
    listed = json.loads(admin.route(cookie_event(steward), "GET", "/api/admin/users")["body"])
    assert [user["subject"] for user in listed["users"]] == ["admin@example.test", "dev@example.test"]
    removed = admin.route(
        cookie_event(steward, method="DELETE",
                     path="/api/admin/users/dev%40example.test", headers=ORIGIN),
        "DELETE", "/api/admin/users/dev%40example.test")
    assert removed["statusCode"] == 200
    remaining = [row["identity"] for row in table.scan()["Items"]]
    assert remaining == ["admin@example.test"]


# --- CLI surface (agent API) ------------------------------------------------

def agent_event(monkeypatch, subject="subject-1", email="op@example.test"):
    monkeypatch.setattr(agent_api, "authenticate", lambda event: (subject, None))
    return {"headers": {}, "_dtc_claims": {"email": email}}


def test_agent_require_operator_follows_roles(monkeypatch):
    operator_env(monkeypatch)
    table = roles_env(monkeypatch, [
        {"identity": "subject-7", "role": "editor"},
        {"identity": "gone@example.test", "role": "disabled"},
    ])
    # Allowlisted operator on an empty store: admin, so user management works.
    bootstrap = agent_event(monkeypatch)
    table.delete_item(Key={"identity": "subject-7"})
    table.delete_item(Key={"identity": "gone@example.test"})
    assert agent_api.require_operator(bootstrap, "users")[0] == "subject-1"
    # Restore rows: the editor widens; users management narrows to operator.
    table.put_item(Item={"identity": "subject-7", "role": "editor"})
    table.put_item(Item={"identity": "gone@example.test", "role": "disabled"})
    editor = agent_event(monkeypatch, subject="subject-7", email="user@example.test")
    assert agent_api.require_operator(editor, "workflow.save")[0] == "subject-7"
    assert agent_api.require_operator(editor, "runs")[0] == "subject-7"
    denied, response = agent_api.require_operator(editor, "grant")
    assert denied is None and response["statusCode"] == 403
    assert json.loads(response["body"])["error"] == "This action needs the 'operator' role"
    admin_denied, response = agent_api.require_operator(editor, "users")
    assert admin_denied is None and response["statusCode"] == 403
    assert json.loads(response["body"])["error"] == "This action needs the 'admin' role"


def test_agent_disabled_and_api_tokens_are_denied(monkeypatch):
    operator_env(monkeypatch)
    roles_env(monkeypatch, [
        {"identity": "gone@example.test", "role": "editor", "disabled": True},
    ])
    disabled = agent_event(monkeypatch, subject="subject-8", email="gone@example.test")
    denied, response = agent_api.require_operator(disabled, "runs")
    assert denied is None and response["statusCode"] == 403
    assert json.loads(response["body"])["error"] == "This account is disabled"
    # An API token never carries a role, even with an assignment stored.
    token_event = agent_event(monkeypatch, subject="token:build",
                              email="machine@example.test")
    token_event["_api_token"] = {"subject": "token:build", "agent": "build"}
    denied, response = agent_api.require_operator(token_event, "workflow.save")
    assert denied is None and response["statusCode"] == 403


def test_agent_users_api_roundtrip(monkeypatch):
    operator_env(monkeypatch)
    table = roles_env(monkeypatch, [], audit=True)
    bootstrap = agent_event(monkeypatch)
    # Bootstrap the first admin while the store is empty.
    first = agent_api.users_api({**bootstrap, "body": json.dumps(
        {"subject": "Admin@Example.test", "role": "admin"})}, "POST")
    assert first["statusCode"] == 200
    # Continue as the stored admin.
    steward = agent_event(monkeypatch, subject="subject-2", email="admin@example.test")
    put = agent_api.users_api({**steward, "body": json.dumps(
        {"subject": "Dev@Example.test", "role": "viewer"})}, "POST")
    assert put["statusCode"] == 200
    listed = json.loads(agent_api.users_api(steward, "GET")["body"])
    assert [user["subject"] for user in listed["users"]] == ["admin@example.test", "dev@example.test"]
    delete = agent_api.users_api({**steward, "queryStringParameters":
                                  {"subject": "dev@example.test"}}, "DELETE")
    assert delete["statusCode"] == 200
    remaining = [row["identity"] for row in table.scan()["Items"]]
    assert remaining == ["admin@example.test"]


# --- CLI commands -----------------------------------------------------------

def test_cli_users_commands_hit_agent_endpoints(monkeypatch, capsys):
    from dapier_cli import commands

    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path, body))
        if method == "GET":
            return {"users": [{"subject": "dev@example.test", "role": "editor",
                               "assigned_by": "op@example.test", "updated_at": "now"}]}
        if method == "POST":
            return {"subject": body["subject"], "role": body["role"]}
        return {"ok": True, "subject": body if body else "x"}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.users_list("https://api.example.test") == 0
    assert commands.users_set_role("https://api.example.test", "dev@example.test",
                                   "editor", display_name="Dev") == 0
    assert commands.users_remove("https://api.example.test", "dev@example.test",
                                 assume_yes=True) == 0
    assert calls == [
        ("GET", "/api/agent/users", None),
        ("POST", "/api/agent/users",
         {"subject": "dev@example.test", "role": "editor", "display_name": "Dev"}),
        ("DELETE", "/api/agent/users?subject=dev%40example.test", None),
    ]
    out, _ = capsys.readouterr()
    assert "dev@example.test" in out
    assert "editor" in out
