"""Connection usage map + connection delete: which flows reference a
connection, and removing a connection outright without breaking the ones
that still use it."""

import json

import pytest

from src.dapier.connections import records as connections
from src.dapier.triggers import connection_usage


def workflow_item(workflow_id, *, enabled=True, actions=None, trigger=None, flow=None):
    return {
        "workflow_id": workflow_id,
        "workflow": {
            "id": workflow_id,
            "enabled": enabled,
            "trigger": trigger or {"connector": "custom", "event": "received"},
            **({"actions": actions} if actions else {}),
            **({"flow": flow} if flow else {}),
        },
    }


def test_collect_walks_trigger_actions_and_flow_nodes():
    items = [
        workflow_item(
            "used-everywhere",
            actions=[
                {"type": "sheets_append", "connection_id": "google-sheets"},
                {"type": "drive_upload", "connection_id": "google-drive"},
            ],
            trigger={"connector": "calendar", "event": "event.started",
                     "connection_id": "google-calendar"},
            flow={"nodes": [{"step": {"type": "slack_post", "connection_id": "slack"}}]},
        ),
        workflow_item("unused", actions=[{"type": "webhook", "url": "https://x"}]),
    ]
    usage = connection_usage.collect(workflows=items, hooks=[])

    assert set(usage) == {"google-sheets", "google-drive", "google-calendar", "slack"}
    sheets = usage["google-sheets"][0]
    assert sheets == {"ref": "used-everywhere", "kind": "workflow",
                      "enabled": True, "where": "step"}
    assert usage["google-calendar"][0]["where"] == "trigger"
    assert usage["slack"][0]["where"] == "step"


def test_collect_deduplicates_and_keeps_disabled_flags():
    items = [
        workflow_item("two-refs", enabled=False, actions=[
            {"type": "dropbox_upload", "connection_id": "dropbox"},
            {"type": "dropbox_delete", "connection_id": "dropbox"},
        ]),
    ]
    usage = connection_usage.collect(workflows=items, hooks=[])
    assert usage["dropbox"] == [{"ref": "two-refs", "kind": "workflow",
                                 "enabled": False, "where": "step"}]


def test_collect_counts_hook_bindings():
    usage = connection_usage.collect(
        workflows=[],
        hooks=[{"hook_id": "todo-bot", "connection_id": "telegram-bot",
                "enabled": True}],
    )
    assert usage["telegram-bot"] == [{"ref": "todo-bot", "kind": "hook",
                                      "enabled": True, "where": "hook"}]


def test_attach_stamps_every_row_even_without_usage():
    rows = [{"connection_id": "a"}, {"connection_id": "b"}]
    connection_usage.attach(rows, usage={"a": [{"ref": "wf", "kind": "workflow",
                                                "enabled": True, "where": "step"}]})
    assert rows[0]["used_in"][0]["ref"] == "wf"
    assert rows[1]["used_in"] == []


class FakeTable:
    """Connections- and grants-shaped fake: plain hash keys plus the
    (connection_id, grantee) composite the grants table uses."""

    def __init__(self, items=None):
        self.items = items or {}

    def get_item(self, *, Key):
        key = Key.get("connection_id")
        item = self.items.get(key)
        return {"Item": dict(item)} if item else {}

    def put_item(self, *, Item):
        self.items[Item.get("connection_id") or Item.get("credential_id")] = Item

    def delete_item(self, *, Key):
        self.items.pop(Key.get("connection_id") or Key.get("credential_id"), None)

    def scan(self, *, Limit=None, **_):
        return {"Items": list(self.items.values())[:Limit] if Limit else list(self.items.values())}


def _connection(connection_id="google-drive", status="ready"):
    return {
        "connection_id": connection_id,
        "provider": "google",
        "display_name": connection_id,
        "scopes": [],
        "granted_scopes": [],
        "status": status,
        "version": 3,
        "updated_at": "now",
    }


def test_api_delete_connection_404_unknown():
    status, payload = connections.api_delete_connection(FakeTable(), "nope", usage={})
    assert status == 404 and "not found" in payload["error"].lower()


def test_api_delete_connection_refuses_used_connection():
    table = FakeTable({"google-sheets": _connection("google-sheets", "connected")})
    usage = {"google-sheets": [{"ref": "todo-intake", "kind": "workflow",
                                "enabled": True, "where": "step"}]}
    status, payload = connections.api_delete_connection(table, "google-sheets",
                                                        usage=usage)
    assert status == 409
    assert "todo-intake" in payload["error"]
    assert payload["used_in"] == usage["google-sheets"]
    assert "google-sheets" in table.items  # nothing was removed


def test_api_delete_connection_force_removes_record_grants_and_credential(
        monkeypatch):
    table = FakeTable({
        "google-sheets": _connection("google-sheets", "connected"),
        "oauth#google-sheets": {"credential_id": "oauth#google-sheets",
                                "value": {"refresh_token": "1//secret"}},
    })
    grants = FakeTable({
        "google-sheets": [
            {"connection_id": "google-sheets", "grantee": "agent-1#a",
             "subject": "agent-1", "agent": "a", "operations": ["use"]},
            {"connection_id": "google-sheets", "grantee": "agent-2#b",
             "subject": "agent-2", "agent": "b", "operations": ["use"]},
        ],
        "other": [
            {"connection_id": "other", "grantee": "agent-1#a",
             "subject": "agent-1", "agent": "a", "operations": ["use"]},
        ],
    })

    import src.dapier.auth.authz as authz
    monkeypatch.setattr(authz, "list_grants",
                        lambda tbl, connection_id=None, limit=None:
                        [item for item in tbl.items.get(connection_id, [])
                         if not connection_id or item["connection_id"] == connection_id])
    monkeypatch.setattr(authz, "delete_grant",
                        lambda tbl, *, connection_id, grantee_id:
                        tbl.items.__getitem__(connection_id).remove(
                            next(item for item in tbl.items[connection_id]
                                 if item["grantee"] == grantee_id)))
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setattr("src.dapier.connections.credentials._table",
                        lambda: FakeTable(table.items))

    status, payload = connections.api_delete_connection(
        table, "google-sheets", grants_table_ref=grants,
        usage={"google-sheets": [{"ref": "wf", "kind": "workflow",
                                  "enabled": True, "where": "step"}]},
        force=True)
    assert status == 200
    assert payload == {"connection_id": "google-sheets", "deleted": True,
                       "grants_removed": 2}
    assert "google-sheets" not in table.items
    # the stored credential went with the connection
    credentials_table = table.items
    assert "oauth#google-sheets" not in credentials_table
    # the other connection's grant survived
    assert len(grants.items["other"]) == 1


def test_api_delete_connection_removes_unused_stub_without_revoke(monkeypatch):
    """A never-consented stub has no token: the delete path must not call
    the provider-revoke flow at all (it would mint an empty credential)."""
    table = FakeTable({"google-calendar-2": _connection("google-calendar-2")})
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    deleted = []

    import src.dapier.connections.credentials as credentials
    monkeypatch.setattr(credentials, "_table",
                        lambda: FakeTable({"sentinel": "keep"}))
    monkeypatch.setattr(credentials, "delete_credential",
                        lambda credential_id: deleted.append(credential_id))

    from src.dapier.connections import tokens as tokens_module
    def explode(connection):
        raise AssertionError("revoke_connection must not run for a stub")
    monkeypatch.setattr(tokens_module, "revoke_connection", explode)

    status, payload = connections.api_delete_connection(
        table, "google-calendar-2", usage={})
    assert status == 200 and payload["deleted"] is True
    assert deleted == ["oauth#google-calendar-2"]
    assert "google-calendar-2" not in table.items


# --- agent API route: operator-gated delete + usage-stamped list ---

def test_agent_delete_route_and_list_usage(monkeypatch):
    from src.dapier.api import agent as agent_api

    connections_items = {
        "google-calendar-2": _connection("google-calendar-2"),
        "google-sheets": _connection("google-sheets", "connected"),
    }
    grants_items = {
        ("google-sheets", "agent-1#a"): {
            "connection_id": "google-sheets", "grantee": "agent-1#a",
            "subject": "agent-1", "agent": "a", "operations": ["use"]},
    }
    tables = {
        "connections": _AgentTable(connections_items),
        "grants": _AgentTable(grants_items),
        "credentials": _AgentTable(),
        "api-tokens": _AgentTable(),
    }

    import boto3

    class Dynamo:
        def Table(self, name):
            return tables[name]

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setenv("DAPIER_SKIP_CONFIG_DB", "1")
    monkeypatch.delenv("HOOK_TRIGGERS_TABLE", raising=False)
    monkeypatch.delenv("WORKFLOWS_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {
                            "sub": "op-1", "email": "op@datatalks.club"})
    monkeypatch.setattr(agent_api.roles, "effective_role",
                        lambda payload: "operator")

    # the operator list stamps used_in onto every row
    monkeypatch.setattr(connection_usage, "collect", lambda: {
        "google-sheets": [{"ref": "todo-intake", "kind": "workflow",
                           "enabled": True, "where": "step"}]})
    listed = agent_api.route(
        _bearer_event(query={"all": "true"}), "GET", "/api/agent/connections")
    assert listed["statusCode"] == 200
    rows = {row["connection_id"]: row
            for row in json.loads(listed["body"])["connections"]}
    assert rows["google-sheets"]["used_in"][0]["ref"] == "todo-intake"
    assert rows["google-calendar-2"]["used_in"] == []

    # deleting the unused stub goes through; the used one is a 409
    deleted = agent_api.route(
        _bearer_event(), "DELETE", "/api/agent/connections/google-calendar-2")
    assert deleted["statusCode"] == 200
    assert "google-calendar-2" not in tables["connections"].items

    refused = agent_api.route(
        _bearer_event(), "DELETE", "/api/agent/connections/google-sheets")
    assert refused["statusCode"] == 409
    assert "todo-intake" in json.loads(refused["body"])["error"]
    assert "google-sheets" in tables["connections"].items

    forced = agent_api.route(
        _bearer_event(query={"force": "1"}), "DELETE",
        "/api/agent/connections/google-sheets")
    assert forced["statusCode"] == 200
    assert json.loads(forced["body"])["grants_removed"] == 1
    assert "google-sheets" not in tables["connections"].items
    assert not tables["grants"].items

    unknown = agent_api.route(_bearer_event(), "DELETE", "/api/agent/connections/nope")
    assert unknown["statusCode"] == 404


def test_agent_delete_route_rejects_non_operators(monkeypatch):
    from src.dapier.api import agent as agent_api

    tables = {
        "connections": _AgentTable({"a": _connection("a")}),
        "grants": _AgentTable(),
        "credentials": _AgentTable(),
        "api-tokens": _AgentTable(),
    }

    import boto3

    class Dynamo:
        def Table(self, name):
            return tables[name]

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("DAPIER_SKIP_CONFIG_DB", "1")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(agent_api, "verify_id_token",
                        lambda token, audience=None: {
                            "sub": "rand-1", "email": "rand@example.test"})
    monkeypatch.setattr(agent_api.roles, "effective_role",
                        lambda payload: "none")

    response = agent_api.route(_bearer_event(), "DELETE", "/api/agent/connections/a")
    assert response["statusCode"] == 403
    assert "a" in tables["connections"].items


class _AgentTable:
    """The agent-API test fake: composite grant keys, plain connection keys."""

    def __init__(self, items=None):
        self.items = items or {}

    def _lookup(self, key):
        if "grantee" in key:
            return self.items.get((key.get("connection_id"), key.get("grantee")))
        return self.items.get(key.get("connection_id"), self.items.get(key.get("credential_id")))

    def get_item(self, **kwargs):
        item = self._lookup(kwargs["Key"])
        return {"Item": dict(item)} if item else {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        if "grantee" in item:
            self.items[(item["connection_id"], item["grantee"])] = item
        else:
            self.items[item.get("connection_id") or item.get("credential_id")
                       or item.get("token_hash")] = item

    def delete_item(self, **kwargs):
        key = kwargs["Key"]
        if "grantee" in key:
            self.items.pop((key["connection_id"], key["grantee"]), None)
        else:
            self.items.pop(key.get("connection_id") or key.get("credential_id"), None)

    def query(self, **kwargs):
        value = kwargs["ExpressionAttributeValues"][":connection"]
        return {"Items": [dict(item) for item in self.items.values()
                          if item.get("connection_id") == value]}

    def scan(self, **_):
        composite = [item for key, item in self.items.items()
                     if isinstance(key, tuple)]
        return {"Items": composite or list(self.items.values())}


def _bearer_event(body=None, query=None):
    request = {"headers": {"host": "dapier.example.test",
                           "authorization": "Bearer dtc-id-token"}, "cookies": []}
    if body is not None:
        request["body"] = json.dumps(body)
    if query is not None:
        request["queryStringParameters"] = query
    return request


# --- CLI ---

def test_cli_delete_reports_and_forces(monkeypatch, capsys):
    from dapier_cli import api as cli_api
    from dapier_cli import commands

    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append((method, path))
        if path.endswith("google-sheets") and "force" not in path:
            raise cli_api.ApiError(
                "Connection google-sheets is still used by: todo-intake. "
                "Remove those references first, or delete with force.",
                status=409)
        return {"connection_id": "google-sheets", "deleted": True,
                "grants_removed": 2}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_delete("https://api.example.test", "google-sheets") == 1
    out, _ = capsys.readouterr()
    assert "todo-intake" in out and "--force" in out

    assert commands.connections_delete("https://api.example.test", "google-sheets",
                                       force=True) == 0
    out, _ = capsys.readouterr()
    assert "Deleted google-sheets" in out and "2 grants removed" in out
    assert calls[-1] == ("DELETE", "/api/agent/connections/google-sheets?force=true")


def test_cli_list_prints_usage_column(monkeypatch, capsys):
    from dapier_cli import commands

    def fake_call(api_url, method, path, body=None, **kwargs):
        return {"connections": [
            {"connection_id": "google-sheets", "provider": "google",
             "status": "connected", "used_in": [
                 {"ref": "todo-intake", "kind": "workflow", "enabled": True,
                  "where": "step"},
                 {"ref": "template-email-to-sheets", "kind": "workflow",
                  "enabled": False, "where": "step"},
                 {"ref": "third", "kind": "workflow", "enabled": True,
                  "where": "step"}]},
            {"connection_id": "google-calendar-2", "provider": "google",
             "status": "ready", "used_in": []},
        ]}

    monkeypatch.setattr(commands.api, "call", fake_call)
    assert commands.connections_list("https://api.example.test", list_all=True) == 0
    out, _ = capsys.readouterr()
    assert "USED IN" in out
    assert "template-email-to-sheets, third +1" in out
    assert "-" in out  # the unused stub


def test_cli_delete_parser_wires_force():
    from dapier_cli import main as cli_main

    args = cli_main.build_parser().parse_args(
        ["connections", "delete", "google-drive", "--force"])
    assert args.command == "delete"
    assert args.connection_id == "google-drive"
    assert args.force is True


@pytest.mark.parametrize("bad", ["", "Bad Id"])
def test_api_delete_connection_validates_id(bad):
    status, payload = connections.api_delete_connection(FakeTable(), bad, usage={})
    assert status == 400
    assert "invalid" in payload["error"].lower()
