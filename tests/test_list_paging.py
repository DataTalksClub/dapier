"""Paging for the connections and grants lists (>50 connections and >100
grants must stay reachable across pages), the agent grant-filtered list's
new limit/next params and operator `?all=true` mode, the console's
GET /api/admin/connections endpoint, and the CLI flags that wrap them."""

import base64
import json
import time

import boto3
import pytest

from dapier_cli import commands as cli_commands
from dapier_cli import main as cli_main
from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.auth import authz, session
from src.dapier.connections import records as connections

# --- fakes ------------------------------------------------------------------

class PagedConnectionTable:
    """DynamoDB-flavoured connections table: paging keyed by connection_id."""

    def __init__(self):
        self.items = {}

    def put_item(self, *, Item):
        self.items[Item["connection_id"]] = Item

    def get_item(self, *, Key):
        item = self.items.get(Key["connection_id"])
        return {"Item": item} if item else {}

    def scan(self, **kwargs):
        ordered = [self.items[key] for key in sorted(self.items)]
        start = kwargs.get("ExclusiveStartKey")
        if start:
            ordered = [item for item in ordered
                       if item["connection_id"] > start["connection_id"]]
        limit = kwargs.get("Limit")
        page = ordered[:limit] if limit else ordered
        result = {"Items": page}
        if limit and len(ordered) > limit:
            result["LastEvaluatedKey"] = {"connection_id": page[-1]["connection_id"]}
        return result


class PagedGrantTable:
    """DynamoDB-flavoured grants table: paging keyed by (connection_id, grantee)."""

    def __init__(self):
        self.items = {}

    @staticmethod
    def _key(item):
        return (item["connection_id"], item["grantee"])

    def put_item(self, *, Item):
        self.items[self._key(Item)] = Item

    def get_item(self, *, Key):
        item = self.items.get((Key["connection_id"], Key["grantee"]))
        return {"Item": dict(item)} if item else {}

    def _window(self, start):
        ordered = [self.items[key] for key in sorted(self.items)]
        if start:
            ordered = [item for item in ordered
                       if (item["connection_id"], item["grantee"]) >
                       (start["connection_id"], start["grantee"])]
        return ordered

    def _page(self, ordered, kwargs):
        limit = kwargs.get("Limit")
        page = ordered[:limit] if limit else ordered
        result = {"Items": page}
        if limit and len(ordered) > limit:
            result["LastEvaluatedKey"] = {
                "connection_id": page[-1]["connection_id"],
                "grantee": page[-1]["grantee"],
            }
        return result

    def scan(self, **kwargs):
        return self._page(self._window(kwargs.get("ExclusiveStartKey")), kwargs)

    def query(self, **kwargs):
        values = list(kwargs.get("ExpressionAttributeValues", {}).values())
        ordered = [item for item in self._window(kwargs.get("ExclusiveStartKey"))
                   if item.get("connection_id") in values]
        return self._page(ordered, kwargs)


def seed_connections(table, count, prefix="conn"):
    for index in range(count):
        table.put_item(Item={
            "connection_id": f"{prefix}-{index:03d}",
            "provider": "slack",
            "display_name": f"{prefix}-{index:03d}",
            "scopes": [],
            "status": "ready",
        })


def seed_grants(table, count, subject="subject-1"):
    for index in range(count):
        authz.put_grant(
            table, connection_id=f"conn-{index:03d}", subject=subject,
            agent="agent-a", operations=["use"], granted_by="op",
        )


# --- grants paging (authz) ---------------------------------------------------

def test_grants_list_pages_past_100():
    table = PagedGrantTable()
    seed_grants(table, 130)
    seen, token = [], None
    for _ in range(10):
        status, payload = authz.api_list_grants(table, limit=50, next_token=token)
        assert status == 200
        seen.extend(payload["grants"])
        token = payload["paging"]["next"]
        if not token:
            break
    assert len(seen) == 130
    keys = [(grant["connection_id"], grant["grantee"]) for grant in seen]
    assert keys == sorted(keys)


def test_grants_list_single_connection_paging():
    table = PagedGrantTable()
    for agent_index in range(7):
        authz.put_grant(
            table, connection_id="youtube-personal",
            subject=f"subject-{agent_index}", agent=f"agent-{agent_index}",
            operations=["use"], granted_by="op",
        )
    status, first = authz.api_list_grants(table, connection_id="youtube-personal", limit=3)
    assert status == 200
    assert len(first["grants"]) == 3 and first["paging"]["next"]
    status, second = authz.api_list_grants(
        table, connection_id="youtube-personal", limit=3,
        next_token=first["paging"]["next"])
    assert status == 200
    assert len(second["grants"]) == 3
    all_grantees = [grant["grantee"] for grant in first["grants"] + second["grants"]]
    assert len(set(all_grantees)) == 6  # pages never overlap


def test_grants_list_rejects_invalid_token():
    status, payload = authz.api_list_grants(PagedGrantTable(), next_token="bogus!!")
    assert status == 400
    assert "token" in payload["error"].lower()


def test_list_grants_backward_compatible_full_sorted_walk():
    table = PagedGrantTable()
    seed_grants(table, 150)
    items = authz.list_grants(table)  # legacy call: no limit, plain list back
    assert len(items) == 150
    keys = [(item["connection_id"], item["grantee"]) for item in items]
    assert keys == sorted(keys)
    assert len(authz.list_grants(table, connection_id="conn-005")) == 1


def test_paging_token_round_trip():
    table = PagedGrantTable()
    seed_grants(table, 5)
    _, payload = authz.api_list_grants(table, limit=2)
    text = payload["paging"]["next"]
    padded = text + "=" * (-len(text) % 4)
    assert json.loads(base64.urlsafe_b64decode(padded)) == {
        "c": "conn-001", "g": "subject-1#agent-a",
    }
    assert authz.decode_paging_token(text) == ("conn-001", "subject-1#agent-a")


# --- agent connections list ---------------------------------------------------

def agent_request(query=None, email="op@example.test"):
    request = {
        "requestContext": {"http": {"method": "GET", "path": "/api/agent/connections"}},
        "headers": {"host": "dapier.example.test", "authorization": "Bearer dtc-token"},
        "cookies": [],
        "_dtc_claims": {"sub": "subject-1", "email": email},
    }
    if query:
        request["queryStringParameters"] = query
    return request


@pytest.fixture
def agent_env(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAILS", "op@example.test")
    monkeypatch.setattr(agent_api, "authenticate", lambda event: ("subject-1", None))
    monkeypatch.setattr(agent_api.tokens, "stored_value", lambda connection_id: {})
    connections_table = PagedConnectionTable()
    seed_connections(connections_table, 55)
    grants_table = PagedGrantTable()
    for index in (0, 1, 2):
        authz.put_grant(
            grants_table, connection_id=f"conn-{index:03d}", subject="subject-1",
            agent="agent-a", operations=["use"], granted_by="op",
        )
    monkeypatch.setattr(agent_api, "_tables", lambda: (connections_table, grants_table))
    monkeypatch.setattr(agent_api.overview, "_connection_views",
                        lambda items: [connections.public_view(item) for item in items])
    return connections_table, grants_table


def body_of(response):
    return json.loads(response["body"])


def test_agent_list_default_shape_unchanged(agent_env):
    response = agent_api.list_for_caller(agent_request())
    assert response["statusCode"] == 200
    payload = body_of(response)
    assert set(payload) == {"connections"}  # no paging block without params
    assert [view["connection_id"] for view in payload["connections"]] == [
        "conn-000", "conn-001", "conn-002"]


def test_agent_list_paged_round_trip(agent_env):
    first = body_of(agent_api.list_for_caller(agent_request({"limit": "2"})))
    assert [view["connection_id"] for view in first["connections"]] == [
        "conn-000", "conn-001"]
    assert first["paging"]["next"] and first["paging"]["limit"] == 2
    second = body_of(agent_api.list_for_caller(
        agent_request({"limit": "2", "next": first["paging"]["next"]})))
    assert [view["connection_id"] for view in second["connections"]] == ["conn-002"]
    assert second["paging"]["next"] is None


def test_agent_list_rejects_invalid_token(agent_env):
    response = agent_api.list_for_caller(agent_request({"limit": "2", "next": "junk!!"}))
    assert response["statusCode"] == 400


def test_agent_all_requires_operator(agent_env, monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAILS", "boss@example.test")
    response = agent_api.list_for_caller(agent_request({"all": "true"}))
    assert response["statusCode"] == 403


def test_agent_all_lists_every_connection_paged(agent_env):
    first = body_of(agent_api.list_for_caller(agent_request({"all": "true"})))
    assert len(first["connections"]) == 50  # the API's default page, not the 3 grants
    assert first["paging"]["next"]
    second = body_of(agent_api.list_for_caller(
        agent_request({"all": "true", "next": first["paging"]["next"]})))
    assert len(first["connections"]) + len(second["connections"]) == 55
    ids = [view["connection_id"] for view in first["connections"] + second["connections"]]
    assert ids == sorted(set(ids))


# --- console admin endpoint ---------------------------------------------------

def cookie_event(token, query=None):
    event = {
        "requestContext": {"http": {"method": "GET", "path": "/api/admin/connections"}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [f"dapier_session={token}"],
    }
    if query:
        event["queryStringParameters"] = query
    return event


def test_admin_connections_list_endpoint(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAILS", "op@example.test")
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "session-secret"})
    token = session._sign({"sub": "op@example.test", "subject": "subject-1",
                           "exp": int(time.time()) + 3600})
    connections_table = PagedConnectionTable()
    seed_connections(connections_table, 55)

    class Dynamo:
        def Table(self, name):
            return {"connections": connections_table}[name]

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")

    unauthenticated = admin.route(cookie_event("bogus"), "GET", "/api/admin/connections")
    assert unauthenticated["statusCode"] == 401

    first = body_of(admin.route(
        cookie_event(token, {"limit": "50"}), "GET", "/api/admin/connections"))
    assert len(first["connections"]) == 50
    assert first["paging"]["next"] and first["paging"]["limit"] == 50
    second = body_of(admin.route(
        cookie_event(token, {"limit": "50", "next": first["paging"]["next"]}),
        "GET", "/api/admin/connections"))
    assert len(second["connections"]) == 5
    assert second["paging"]["next"] is None
    ids = [view["connection_id"] for view in first["connections"] + second["connections"]]
    assert ids == sorted(set(ids))
    # Rows are the console's public view: metadata plus health, never secrets.
    assert "credential_id" not in first["connections"][0]
    assert "health" in first["connections"][0]


# --- CLI -----------------------------------------------------------------------

def test_cli_connections_and_grants_paging_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli_commands, "connections_list",
        lambda api_url, debug=False, **kwargs: seen.update(conn=kwargs) or 0)
    assert cli_main.main(
        ["connections", "list", "--limit", "50", "--next", "tok", "--all"]) == 0
    assert seen["conn"] == {"limit": 50, "next_token": "tok", "list_all": True}
    assert cli_main.main(["connections", "list"]) == 0
    assert seen["conn"] == {"limit": None, "next_token": None, "list_all": False}

    monkeypatch.setattr(
        cli_commands, "grants_list",
        lambda api_url, connection_id=None, debug=False, **kwargs:
        seen.update(grants={"connection": connection_id, **kwargs}) or 0)
    assert cli_main.main(["grants", "list", "--connection", "youtube-personal",
                          "--limit", "10"]) == 0
    assert seen["grants"] == {"connection": "youtube-personal",
                              "limit": 10, "next_token": None}


def test_cli_list_commands_call_paged_endpoints(monkeypatch, capsys):
    calls = []

    def fake_call(api_url, method, path, body=None, **kwargs):
        calls.append(path)
        return {"connections": [], "grants": [], "paging": {}}

    monkeypatch.setattr(cli_commands.api, "call", fake_call)
    assert cli_commands.connections_list("https://api.example.test", list_all=True) == 0
    assert calls[-1] == "/api/agent/connections?all=true"
    assert cli_commands.connections_list(
        "https://api.example.test", limit=50, next_token="abc") == 0
    assert calls[-1] == "/api/agent/connections?limit=50&next=abc"
    assert cli_commands.grants_list("https://api.example.test", limit=10) == 0
    assert calls[-1] == "/api/agent/grants?limit=10"
    assert cli_commands.grants_list(
        "https://api.example.test", connection_id="c", next_token="xyz") == 0
    assert calls[-1] == "/api/agent/grants?connection_id=c&next=xyz"
