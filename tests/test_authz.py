import json
import time

import pytest

from src.dapier.api import admin
from src.dapier import audit
from src.dapier.auth import authz
import boto3
from src.dapier.auth import session


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


class DictTable:
    def __init__(self, key_fields):
        self.key_fields = key_fields
        self.items = {}
        self.writes = []

    def _key(self, item_or_key):
        return tuple(item_or_key[field] for field in self.key_fields)

    def put_item(self, **kwargs):
        self.writes.append(kwargs["Item"])
        self.items[self._key(kwargs["Item"])] = kwargs["Item"]

    def get_item(self, **kwargs):
        item = self.items.get(self._key(kwargs["Key"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, **kwargs):
        self.items.pop(self._key(kwargs["Key"]), None)

    def query(self, **kwargs):
        values = list(kwargs.get("ExpressionAttributeValues", {}).values())
        matched = [item for item in self.items.values() if item.get("connection_id") in values]
        return {"Items": matched}

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


# --- operator checks -------------------------------------------------------

def test_is_operator_matches_subject_or_email(monkeypatch):
    operator_env(monkeypatch, emails="Op@Example.Test", subjects="subject-9")
    assert authz.is_operator({"sub": "op@example.test", "subject": "other"}) is True
    assert authz.is_operator({"sub": "someone@example.test", "subject": "subject-9"}) is True
    assert authz.is_operator({"sub": "stranger@example.test", "subject": "subject-1"}) is False
    assert authz.is_operator({}) is False
    assert authz.is_operator(None) is False


def test_is_operator_allows_any_authenticated_account_by_default(monkeypatch):
    monkeypatch.setenv("OPERATOR_EMAILS", "")
    monkeypatch.setenv("OPERATOR_SUBJECTS", "")
    assert authz.is_operator({"sub": "op@datatalks.club", "subject": "subject-1"}) is True
    assert authz.is_operator({}) is False
    assert authz.is_operator(None) is False


def test_overview_rejects_unauthenticated(monkeypatch):
    monkeypatch.setattr(session, "_credentials", lambda: {"password": "x"})
    event = cookie_event("bogus", headers={})
    event["cookies"] = []
    response = admin.route(event, "GET", "/api/admin/overview")
    assert response["statusCode"] == 401


def test_overview_rejects_ordinary_user(monkeypatch):
    operator_env(monkeypatch, emails="boss@example.test")
    token = session_token(monkeypatch)
    response = admin.route(cookie_event(token), "GET", "/api/admin/overview")
    assert response["statusCode"] == 403
    assert "Operator" in json.loads(response["body"])["error"]


def test_overview_allows_operator(monkeypatch, tmp_path):
    operator_env(monkeypatch)
    token = session_token(monkeypatch)
    monkeypatch.setenv("WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    dynamo(monkeypatch, {
        "executions": DictTable(("execution_id",)),
        "connections": DictTable(("connection_id",)),
        "credentials": DictTable(("credential_id",)),
        "api-tokens": DictTable(("token_hash",)),
    })
    from src.dapier.connections import credentials as credentials_module

    monkeypatch.setattr(credentials_module.boto3, "resource", boto3.resource)
    monkeypatch.setenv("EXECUTIONS_TABLE", "executions")
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("CREDENTIALS_TABLE", "credentials")
    monkeypatch.setenv("API_TOKENS_TABLE", "api-tokens")
    response = admin.route(cookie_event(token), "GET", "/api/admin/overview")
    assert response["statusCode"] == 200


# --- CSRF ------------------------------------------------------------------

def test_cookie_mutation_without_origin_is_rejected(monkeypatch):
    operator_env(monkeypatch)
    token = session_token(monkeypatch)
    event = cookie_event(token, method="PUT",
                         path="/api/admin/credentials/nosuch", body={"token": "x"})
    response = admin.route(event, "PUT", "/api/admin/credentials/nosuch")
    assert response["statusCode"] == 403


def test_cookie_mutation_with_matching_origin_passes(monkeypatch):
    operator_env(monkeypatch)
    token = session_token(monkeypatch)
    event = cookie_event(token, method="PUT", path="/api/admin/credentials/nosuch",
                         headers={"origin": "https://dapier.example.test"},
                         body={"token": "x"})
    response = admin.route(event, "PUT", "/api/admin/credentials/nosuch")
    assert response["statusCode"] == 404


# --- grants ----------------------------------------------------------------

def grant_tables(monkeypatch):
    tables = {
        "connections": DictTable(("connection_id",)),
        "grants": DictTable(("connection_id", "grantee")),
    }
    tables["connections"].items[("youtube-personal",)] = {
        "connection_id": "youtube-personal", "provider": "youtube",
    }
    dynamo(monkeypatch, tables)
    monkeypatch.setenv("CONNECTIONS_TABLE", "connections")
    monkeypatch.setenv("GRANTS_TABLE", "grants")
    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    return tables


def grant_event(token, method, body=None, query=None):
    event = cookie_event(token, method=method, path="/api/admin/grants",
                         headers={"origin": "https://dapier.example.test"},
                         body=body)
    if query:
        event["queryStringParameters"] = query
    return event


def test_grant_crud_roundtrip(monkeypatch):
    operator_env(monkeypatch)
    tables = grant_tables(monkeypatch)
    token = session_token(monkeypatch)

    put = admin.route(grant_event(token, "PUT", body={
        "connection_id": "youtube-personal", "subject": "subject-1",
        "agent": "buildcamp-uploader", "operations": ["use"],
    }), "PUT", "/api/admin/grants")
    assert put["statusCode"] == 200

    assert authz.check_grant(tables["grants"], subject="subject-1",
                             agent="buildcamp-uploader",
                             connection_id="youtube-personal", operation="use") is True
    # Naming another agent grants nothing.
    assert authz.check_grant(tables["grants"], subject="subject-1",
                             agent="other-agent",
                             connection_id="youtube-personal", operation="use") is False

    listed = admin.route(grant_event(token, "GET"), "GET", "/api/admin/grants")
    assert listed["statusCode"] == 200
    assert len(json.loads(listed["body"])["grants"]) == 1

    deleted = admin.route(grant_event(token, "DELETE", query={
        "connection_id": "youtube-personal", "grantee": "subject-1#buildcamp-uploader",
    }), "DELETE", "/api/admin/grants")
    assert deleted["statusCode"] == 200
    assert authz.check_grant(tables["grants"], subject="subject-1",
                             agent="buildcamp-uploader",
                             connection_id="youtube-personal", operation="use") is False


def test_grant_requires_existing_connection(monkeypatch):
    operator_env(monkeypatch)
    grant_tables(monkeypatch)
    token = session_token(monkeypatch)
    response = admin.route(grant_event(token, "PUT", body={
        "connection_id": "nope", "subject": "subject-1",
        "agent": "agent-x", "operations": ["use"],
    }), "PUT", "/api/admin/grants")
    assert response["statusCode"] == 404


def test_grant_rejects_bad_agent_and_operations(monkeypatch):
    operator_env(monkeypatch)
    grant_tables(monkeypatch)
    token = session_token(monkeypatch)
    for body in (
        {"connection_id": "youtube-personal", "subject": "s", "agent": "Bad Agent!", "operations": ["use"]},
        {"connection_id": "youtube-personal", "subject": "s", "agent": "agent-x", "operations": ["root"]},
    ):
        response = admin.route(grant_event(token, "PUT", body=body), "PUT", "/api/admin/grants")
        assert response["statusCode"] == 400


def test_admin_implies_use_and_expiry_denies(monkeypatch):
    table = DictTable(("connection_id", "grantee"))
    authz.put_grant(table, connection_id="c", subject="s", agent="agent-a",
                    operations=["admin"], granted_by="op")
    assert authz.check_grant(table, subject="s", agent="agent-a", connection_id="c", operation="use") is True
    assert authz.check_grant(table, subject="s", agent="agent-a", connection_id="c", operation="delete") is False

    authz.put_grant(table, connection_id="c2", subject="s", agent="agent-a",
                    operations=["use"], granted_by="op", expires_at=int(time.time()) - 1)
    assert authz.check_grant(table, subject="s", agent="agent-a", connection_id="c2", operation="use") is False


# --- audit -----------------------------------------------------------------

def test_audit_record_shape_ttl_and_redaction():
    table = DictTable(("audit_id",))
    item = audit.record(
        table, connection_id="youtube-personal", action=audit.TOKEN,
        actor_subject="subject-1", agent="buildcamp-uploader", outcome="ok",
        error={"client_secret": "shh", "note": "boom"}, now=1_000_000,
    )
    assert item["expires_at"] == 1_000_000 + 90 * 86400
    assert "shh" not in str(item)
    assert table.items[(item["audit_id"],)]["outcome"] == "ok"


def test_audit_write_failure_does_not_raise():
    class Broken:
        def put_item(self, **kwargs):
            raise RuntimeError("dynamo down")

    item = audit.record(Broken(), connection_id="c", action=audit.TOKEN,
                        actor_subject="s", outcome="ok")
    assert item["connection_id"] == "c"
