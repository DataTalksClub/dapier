"""Discovery through the aws/s3 pseudo-connection.

The stored AWS-keys credential has no connection record, but its bucket
listing and STS health check resolve through the synthetic "aws" connection
(api.discovery._load), so the designer's bucket picker and
``dapier connections discover|test aws`` reach the same surface as every
other provider.
"""
import json

import boto3
import pytest

from src.dapier.api import admin
from src.dapier.api import discovery as discovery_api
from src.dapier.auth import session


@pytest.fixture(autouse=True)
def connections_env(monkeypatch):
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")

    class Dynamo:
        def Table(self, name):
            return Table()

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())


class Table:
    def __init__(self, items=None):
        self.items = items or {}

    def get_item(self, **kwargs):
        item = self.items.get(kwargs["Key"].get("connection_id"))
        return {"Item": dict(item)} if item else {}


def aws_keys(monkeypatch):
    """The stored "aws" credential holds a key pair; s3/STS calls are faked."""
    from src.dapier.connections import credentials as credentials_module

    monkeypatch.setattr(credentials_module, "get_credential",
                        lambda credential_id: {
                            "access_key_id": "AKIDEXAMPLE", "secret_access_key": "secret"})

    class FakeClient:
        def __init__(self, service, **kwargs):
            self.service = service

        def list_buckets(self):
            return {"Buckets": [{"Name": "bucket-a"}, {"Name": "bucket-b"}]}

        def get_caller_identity(self):
            return {"Arn": "arn:aws:iam::123:user/operator", "Account": "123",
                    "UserId": "AIDEXAMPLE"}

    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: FakeClient(service))


def test_pseudo_connection_serves_the_bucket_catalog():
    status, payload = discovery_api.resources("aws")
    assert status == 200
    assert payload["provider"] == "s3"
    assert "buckets" in [resource["name"] for resource in payload["resources"]]


def test_s3_is_an_alias_of_aws(monkeypatch):
    aws_keys(monkeypatch)
    status, payload = discovery_api.discover("s3", "buckets", {})
    assert status == 200
    assert [item["name"] for item in payload["items"]] == ["bucket-a", "bucket-b"]


def test_pseudo_connection_lists_buckets(monkeypatch):
    aws_keys(monkeypatch)
    status, payload = discovery_api.discover("aws", "buckets", {})
    assert status == 200
    assert [item["id"] for item in payload["items"]] == ["bucket-a", "bucket-b"]


def test_unknown_resource_still_404s_for_the_pseudo_connection():
    status, payload = discovery_api.discover("aws", "folders", {})
    assert status == 404


def test_pseudo_connection_health_check_verifies_the_keys(monkeypatch):
    aws_keys(monkeypatch)
    status, payload = discovery_api.test_connection("aws")
    assert status == 200
    assert payload["ok"] is True
    assert "operator" in payload["detail"]


def test_a_real_connection_record_wins_over_the_pseudo_one(monkeypatch):
    stored = {"connection_id": "aws", "provider": "google", "status": "connected",
              "credential_id": "oauth#aws"}
    connection, error = discovery_api._load("aws", Table({"aws": stored}))
    assert error is None
    assert connection["provider"] == "google"


def test_pseudo_connection_still_requires_a_known_name(monkeypatch):
    status, payload = discovery_api.resources("not-a-pseudo-id")
    assert status == 404


# --- admin surface (the designer's bucket picker) ---


def admin_request(method, path, query=None, body=None):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test"},
        "cookies": [],
        "body": json.dumps(body) if body is not None else None,
    }
    if query is not None:
        event["queryStringParameters"] = query
    return event


@pytest.fixture
def admin_session(monkeypatch):
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setattr(session, "authenticated", lambda event: True)
    monkeypatch.setattr(session, "_csrf_ok", lambda event, method: True)
    monkeypatch.setattr(session, "require_operator", lambda event: ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: {"emitted": True})


def test_admin_discover_and_test_the_aws_pseudo_connection(monkeypatch, admin_session):
    aws_keys(monkeypatch)
    catalog = admin.route(
        admin_request("GET", "/api/admin/connections/aws/discover"),
        "GET", "/api/admin/connections/aws/discover")
    assert catalog["statusCode"] == 200
    assert "buckets" in [r["name"] for r in json.loads(catalog["body"])["resources"]]

    items = admin.route(
        admin_request("GET", "/api/admin/connections/aws/discover/buckets"),
        "GET", "/api/admin/connections/aws/discover/buckets")
    assert items["statusCode"] == 200
    assert [item["name"] for item in json.loads(items["body"])["items"]] == \
        ["bucket-a", "bucket-b"]

    verdict = admin.route(
        admin_request("POST", "/api/admin/connections/aws/test", body={}),
        "POST", "/api/admin/connections/aws/test")
    assert verdict["statusCode"] == 200
    assert json.loads(verdict["body"])["ok"] is True


def test_admin_aws_discover_requires_signin(monkeypatch, admin_session):
    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(
        admin_request("GET", "/api/admin/connections/aws/discover"),
        "GET", "/api/admin/connections/aws/discover")
    assert response["statusCode"] == 401


# --- CLI surface ---


def test_cli_routes_aws_discover_and_test_through_the_agent_api(monkeypatch):
    from dapier_cli import commands, main

    calls = []
    monkeypatch.setattr(commands, "connections_discover",
                        lambda api_url, connection_id, resource=None, params=(), debug=False:
                        calls.append(("discover", connection_id, resource)) or 0)
    monkeypatch.setattr(commands, "connections_test",
                        lambda api_url, connection_id, debug=False:
                        calls.append(("test", connection_id)) or 0)
    assert main.main(["connections", "discover", "aws"]) == 0
    assert main.main(["connections", "test", "aws"]) == 0
    assert calls == [("discover", "aws", None), ("test", "aws")]
