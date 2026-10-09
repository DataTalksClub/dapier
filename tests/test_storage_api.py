"""Storage over HTTP: /api/{agent,admin}/storage/{workflow} parity.

The endpoints and the storage_* actions must see the same table and the
same rules, so these tests reuse the actions' fake table and drive the
agent route handler (operator-gated like runs/usage) plus the admin
wrapper the console calls.
"""
import json

import boto3
import pytest

from src.dapier.api import agent as agent_api
from src.dapier.api.admin import routes as admin_routes
from src.dapier.engine.actions import storage

from test_storage_actions import FakeStorageTable


@pytest.fixture()
def storage_table(monkeypatch):
    table = FakeStorageTable()
    monkeypatch.setenv("STORAGE_TABLE", "workflow-state")

    class Dynamo:
        def Table(self, _name):
            return table

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    return table


@pytest.fixture()
def operator(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    agent_api.reset_rate_limits()
    tables = {
        "connections": {}, "grants": {}, "credentials": {}, "api-tokens": {},
    }

    class Dynamo:
        def Table(self, name):
            return tables.get(name) or type("T", (), {"get_item": lambda self, **k: {}})()

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "op-1", "email": "op@datatalks.club"})
    return tables


def agent_event(body=None, query=None):
    request = {
        "headers": {"host": "dapier.example.test",
                    "authorization": "Bearer dtc-id-token"},
        "cookies": [],
    }
    if body is not None:
        request["body"] = json.dumps(body)
    if query is not None:
        request["queryStringParameters"] = query
    return request


def test_get_reads_a_stored_value(operator, storage_table):
    storage.kv_set("wf-1", "cursor", "inbox/42")
    response = agent_api.route(
        agent_event(query={"key": "cursor"}), "GET", "/api/agent/storage/wf-1")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["key"] == "cursor"
    assert body["value"] == "inbox/42"
    assert body["updated_at"]


def test_get_missing_key_is_404(operator, storage_table):
    response = agent_api.route(
        agent_event(query={"key": "nope"}), "GET", "/api/agent/storage/wf-1")
    assert response["statusCode"] == 404


def test_get_without_key_lists_under_prefix(operator, storage_table):
    storage.kv_set("wf-1", "seen/alpha", "1")
    storage.kv_set("wf-1", "seen/beta", "2")
    storage.kv_set("wf-1", "other", "3")
    response = agent_api.route(
        agent_event(query={"prefix": "seen/"}), "GET", "/api/agent/storage/wf-1")
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert [item["key"] for item in body["items"]] == ["seen/alpha", "seen/beta"]
    assert body["count"] == 2


def test_get_without_key_or_prefix_lists_every_key(operator, storage_table):
    # The console lists a workflow's keys with no prefix the moment it is
    # chosen; DynamoDB rejects begins_with(""), so this used to 500.
    storage.kv_set("wf-1", "seen/alpha", "1")
    storage.kv_set("wf-1", "other", "3")
    storage.kv_set("wf-2", "elsewhere", "4")
    response = agent_api.route(agent_event(), "GET", "/api/agent/storage/wf-1")
    body = json.loads(response["body"])
    assert response["statusCode"] == 200
    assert [item["key"] for item in body["items"]] == ["other", "seen/alpha"]
    assert body["prefix"] == ""


def test_post_stores_and_delete_removes(operator, storage_table):
    response = agent_api.route(
        agent_event(body={"key": "cursor", "value": "a", "ttl_seconds": 60}),
        "POST", "/api/agent/storage/wf-1")
    assert response["statusCode"] == 200
    stored = json.loads(response["body"])
    assert stored["stored"] is True
    assert storage_table.items[("wf-1", "cursor")]["value"] == "a"

    response = agent_api.route(
        agent_event(query={"key": "cursor"}), "DELETE", "/api/agent/storage/wf-1")
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["deleted"] is True
    assert ("wf-1", "cursor") not in storage_table.items


def test_blank_key_is_a_client_error_not_a_crash(operator, storage_table):
    response = agent_api.route(agent_event(body={"value": "x"}),
                               "POST", "/api/agent/storage/wf-1")
    assert response["statusCode"] == 400


def test_agent_storage_is_operator_only(storage_table, monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAILS", "op@datatalks.club")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    agent_api.reset_rate_limits()
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: {"sub": "peon-1", "email": "peon@example.test"})
    response = agent_api.route(
        agent_event(query={"key": "cursor"}), "GET", "/api/agent/storage/wf-1")
    assert response["statusCode"] == 403


def test_admin_routes_drive_the_same_domain(operator, storage_table):
    stored = admin_routes.storage_write({"body": json.dumps({"key": "k", "value": "v"})}, "wf-1")
    assert stored["statusCode"] == 200

    read = admin_routes.storage_read({"queryStringParameters": {"key": "k"}}, "wf-1")
    assert json.loads(read["body"])["value"] == "v"

    listed = admin_routes.storage_read({"queryStringParameters": {"prefix": ""}}, "wf-1")
    assert json.loads(listed["body"])["count"] == 1

    removed = admin_routes.storage_delete({"queryStringParameters": {"key": "k"}}, "wf-1")
    assert json.loads(removed["body"])["deleted"] is True


def test_listing_carries_each_keys_expiry(operator, storage_table):
    # The designer's Stored data panel shows when each key expires; the
    # list (not just the single-key read) has to carry it.
    storage.kv_set("wf-1", "temp", "x", 60)
    storage.kv_set("wf-1", "kept", "y")
    listed = admin_routes.storage_read({"queryStringParameters": {}}, "wf-1")
    items = {item["key"]: item for item in json.loads(listed["body"])["items"]}
    assert isinstance(items["temp"]["expires"], int) and items["temp"]["expires"] > 0
    assert items["kept"]["expires"] is None
