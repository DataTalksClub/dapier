"""Connection health tests: POST /api/agent/connections/{cid}/test and its
console mirror under /api/admin/*, plus the ConnectionTest registry the
endpoint serves from.

The verdict always rides HTTP 200 (``ok: false`` carries the failure) so
both UIs render it; the CLI exits nonzero on it. Provider HTTP is faked at
the same seams the other discovery tests use.
"""
import json
import time

import pytest

from src.dapier.api import admin
from src.dapier.api import agent as agent_api
from src.dapier.auth import session
from src.dapier.connections import discovery as provider_discovery
from src.dapier.connections import tokens
from src.dapier.connections.providers import slack_tokens


class FakeTransport:
    """Route provider calls by URL substring to canned JSON responses."""

    def __init__(self, *routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append({"method": method, "url": url})
        for substring, status, payload in self.routes:
            if substring in url:
                return status, json.dumps(payload).encode()
        raise AssertionError(f"unexpected provider call: {method} {url}")


SLACK_CONNECTION = {
    "connection_id": "slack-main", "provider": "slack", "status": "connected",
    "credential_id": "oauth#slack-main",
}
GOOGLE_CONNECTION = {
    "connection_id": "sheets-team", "provider": "google", "status": "connected",
    "credential_id": "oauth#sheets-team",
}
RENDER_CONNECTION = {
    "connection_id": "render", "provider": "render", "status": "connected",
    "credential_id": "oauth#render",
}


# --- API surface: /api/agent/* (CLI bearer) ---


class Table:
    def __init__(self, items=None):
        self.items = items or {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        item = self.items.get(key.get("connection_id") or key.get("credential_id"))
        return {"Item": dict(item)} if item else {}

    def put_item(self, **kwargs):
        item = kwargs["Item"]
        self.items[item.get("connection_id") or item.get("credential_id")] = item

    def scan(self, **kwargs):
        return {"Items": list(self.items.values())}


def configure_agent(monkeypatch, *, claims=None, connections=None, credentials=None):
    agent_api.reset_rate_limits()
    tables = {
        "connections": Table(connections or {}),
        "grants": Table(),
        "credentials": Table(credentials or {}),
        "api-tokens": Table(),
    }

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    monkeypatch.setenv("AUTH_CLI_CLIENT_ID", "cli-client")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.delenv("OPERATOR_EMAILS", raising=False)
    monkeypatch.delenv("OPERATOR_SUBJECTS", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(
        agent_api, "verify_id_token",
        lambda token, audience=None: dict(claims) if claims is not None else (_ for _ in ()).throw(ValueError("bad")),
    )
    return tables


def agent_event(query=None, token="dtc-id-token", body=None):
    headers = {"host": "dapier.example.test"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = {"headers": headers, "cookies": []}
    if query is not None:
        request["queryStringParameters"] = query
    if body is not None:
        request["body"] = json.dumps(body)
    return request


def seed_slack_credential(monkeypatch):
    """A stored Slack token plus an auth.test that answers for it."""
    def fake_verify(token, *, transport=None):
        assert token == "xoxb-fake-token"
        return "T024P01", "DataTalks"

    monkeypatch.setattr(slack_tokens, "verify_account", fake_verify)
    return {"oauth#slack-main": {"credential_id": "oauth#slack-main",
                                 "value": {"token": "xoxb-fake-token"}}}


def test_agent_connection_test_reports_the_slack_workspace(monkeypatch):
    credentials = seed_slack_credential(monkeypatch)
    configure_agent(monkeypatch, claims={"sub": "subject-1"},
                    connections={"slack-main": SLACK_CONNECTION},
                    credentials=credentials)
    response = agent_api.route(
        agent_event(body={}), "POST", "/api/agent/connections/slack-main/test")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["ok"] is True
    assert "DataTalks" in body["detail"]
    assert body["identity"] == {"id": "T024P01", "name": "DataTalks"}


def test_agent_connection_test_unknown_connection_is_404(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1"})
    response = agent_api.route(
        agent_event(body={}), "POST", "/api/agent/connections/nope/test")
    assert response["statusCode"] == 404
    assert "Unknown connection 'nope'" in json.loads(response["body"])["error"]


def test_agent_connection_test_requires_an_operator(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1"},
                    connections={"slack-main": SLACK_CONNECTION})
    monkeypatch.setenv("OPERATOR_EMAILS", "boss@example.test")
    response = agent_api.route(
        agent_event(body={}), "POST", "/api/agent/connections/slack-main/test")
    assert response["statusCode"] == 403


def test_agent_connection_test_provider_failure_is_ok_false(monkeypatch):
    credentials = seed_slack_credential(monkeypatch)

    def reject(token, *, transport=None):
        raise slack_tokens.SlackTokenError("Slack rejected the token: invalid_auth")

    monkeypatch.setattr(slack_tokens, "verify_account", reject)
    configure_agent(monkeypatch, claims={"sub": "subject-1"},
                    connections={"slack-main": SLACK_CONNECTION},
                    credentials=credentials)
    response = agent_api.route(
        agent_event(body={}), "POST", "/api/agent/connections/slack-main/test")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["ok"] is False
    assert "invalid_auth" in body["detail"]
    assert "identity" not in body


def test_agent_connection_test_without_a_handler_never_500s(monkeypatch):
    configure_agent(monkeypatch, claims={"sub": "subject-1"},
                    connections={"render": RENDER_CONNECTION})
    response = agent_api.route(
        agent_event(body={}), "POST", "/api/agent/connections/render/test")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["ok"] is False
    assert body["detail"] == "no health check for this provider"


# --- API surface: /api/admin/* (console session cookie) ---


def configure_admin(monkeypatch, connections=None, credentials=None):
    tables = {
        "connections": Table(connections or {}),
        "credentials": Table(credentials or {}),
    }

    class Dynamo:
        def Table(self, name):
            return tables[name]

    import boto3

    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    monkeypatch.setattr(session, "_credentials",
                        lambda: {"username": "admin", "password": "pw"})
    cookie = session._sign({"sub": "op@datatalks.club", "subject": "op-sub",
                            "exp": int(time.time()) + 600})
    return [f"dapier_session={cookie}"]


def admin_request(method, path, cookies=None, body=None):
    event = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"host": "dapier.example.test",
                    "origin": "https://dapier.example.test"},
        "cookies": cookies or [],
    }
    if body is not None:
        event["body"] = json.dumps(body)
    return event


def test_admin_connection_test_mirrors_the_agent_verdict(monkeypatch):
    credentials = seed_slack_credential(monkeypatch)
    cookies = configure_admin(monkeypatch,
                              connections={"slack-main": SLACK_CONNECTION},
                              credentials=credentials)
    response = admin.route(
        admin_request("POST", "/api/admin/connections/slack-main/test",
                      body={}, cookies=cookies),
        "POST", "/api/admin/connections/slack-main/test")
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["ok"] is True
    assert body["identity"] == {"id": "T024P01", "name": "DataTalks"}


def test_admin_connection_test_unknown_connection_is_404(monkeypatch):
    cookies = configure_admin(monkeypatch)
    response = admin.route(
        admin_request("POST", "/api/admin/connections/nope/test",
                      body={}, cookies=cookies),
        "POST", "/api/admin/connections/nope/test")
    assert response["statusCode"] == 404


def test_admin_connection_test_requires_signin(monkeypatch):
    configure_admin(monkeypatch, connections={"slack-main": SLACK_CONNECTION})
    response = admin.route(
        admin_request("POST", "/api/admin/connections/slack-main/test", body={}),
        "POST", "/api/admin/connections/slack-main/test")
    assert response["statusCode"] == 401


# --- registry: ConnectionTest entries behind both surfaces ---


def test_google_connection_test_refreshes_and_verifies(monkeypatch):
    from src.dapier.connectors import registry

    monkeypatch.setattr(
        tokens, "get_access_token",
        lambda connection, transport=None: ("tok", {"refreshed": True,
                                                    "provider_account_id": "op@x",
                                                    "account_title": "Op"}))

    def verify(provider, token, *, transport=None):
        assert provider == "google" and token == "tok"
        return "op@x", "Operator"

    monkeypatch.setattr(provider_discovery.oauth_providers, "verify_account", verify)
    verdict = registry.CONNECTION_TESTS["google"].run(GOOGLE_CONNECTION)
    assert verdict["ok"] is True
    assert verdict["identity"] == {"id": "op@x", "name": "Operator"}


def test_s3_alias_resolves_the_aws_sts_check(monkeypatch):
    import boto3

    from src.dapier.connectors import registry

    assert registry.connection_test_for("s3") is registry.CONNECTION_TESTS["aws"]

    class FakeSts:
        def get_caller_identity(self):
            return {"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/dapier",
                    "UserId": "AIDEXAMPLE"}

    monkeypatch.setattr(boto3, "client", lambda service, **kwargs: FakeSts())
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    tables = {"credentials": Table({"aws": {"credential_id": "aws", "value": {
        "access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}}})}

    class Dynamo:
        def Table(self, name):
            return tables[name]

    monkeypatch.setattr(boto3, "resource", lambda service: Dynamo())
    verdict = registry.CONNECTION_TESTS["aws"].run({"connection_id": "aws", "credential_id": "aws"})
    assert verdict["ok"] is True
    assert verdict["identity"]["account"] == "123456789012"


def test_every_connection_provider_has_a_registered_check():
    from src.dapier.connectors import registry
    from src.dapier.connections.records import TOKEN_PROVIDERS
    from src.dapier.connections.providers import oauth_providers

    served = set(TOKEN_PROVIDERS) | set(oauth_providers.PROVIDERS)
    assert served <= set(registry.CONNECTION_TESTS)
    for connector, test in registry.CONNECTION_TESTS.items():
        assert test.connector == connector
        assert callable(test.run)
