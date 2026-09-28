"""Discovery, health check and actions behind the Mailchimp credential.

The stored Mailchimp API key has no connection record, but its audience
listing and ping health check resolve through the synthetic "mailchimp"
connection (api.discovery._load), so the designer's audience picker and
``dapier connections discover|test mailchimp`` reach the same surface as
every other provider.
"""
import hashlib
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


EMAIL = "person@example.test"
DIGEST = hashlib.md5(EMAIL.encode()).hexdigest()
MEMBER = {
    "id": DIGEST,
    "email_address": EMAIL,
    "status": "subscribed",
    "merge_fields": {"FNAME": "Person"},
    "list_id": "abc123",
}
LISTS = [{"id": "abc123", "name": "Newsletter", "stats": {"member_count": 1}}]


class FakeMailchimp:
    """The Marketing API: ping, lists, and one audience's members."""

    def __init__(self, *, ping_status=200, member_exists=True):
        self.calls = []
        self.ping_status = ping_status
        self.member_exists = member_exists
        self.upserts = []

    def __call__(self, method, url, *, headers=None, body=None, timeout=15):
        self.calls.append((method, url, headers or {}, body))
        assert (headers or {}).get("authorization", "").startswith("Basic ")
        path = url.split(".api.mailchimp.com/3.0", 1)[-1].split("?")[0]
        if path == "/ping":
            return self.ping_status, b'{"health_status": "Everything\'s Chimpy!"}'
        if path == "/lists":
            return 200, json.dumps({"lists": LISTS}).encode()
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[2] == "members":
            return 200, json.dumps({"members": [MEMBER] if self.member_exists else []}).encode()
        if len(parts) == 4 and parts[2] == "members":
            if method == "GET":
                if self.member_exists:
                    return 200, json.dumps(MEMBER).encode()
                return 404, b'{"title": "Resource Not Found"}'
            self.upserts.append(json.loads(body or b"{}"))
            return 200, json.dumps(MEMBER).encode()
        return 404, b"{}"


@pytest.fixture
def mailchimp_cred(monkeypatch):
    from src.dapier.connections import credentials as credentials_module

    monkeypatch.setattr(credentials_module, "get_credential",
                        lambda credential_id: {"apiKey": "key123-us12", "server": "us12"})


def fake_transport(monkeypatch, fake):
    from src.dapier.engine.actions import base

    monkeypatch.setattr(base, "_default_transport", fake)
    return fake


# --- discovery + health check through the pseudo connection ---


def test_pseudo_connection_serves_the_mailchimp_catalog():
    status, payload = discovery_api.resources("mailchimp")
    assert status == 200
    assert payload["provider"] == "mailchimp"
    assert {"audiences", "members"} <= {r["name"] for r in payload["resources"]}


def test_pseudo_connection_lists_audiences(monkeypatch, mailchimp_cred):
    fake_transport(monkeypatch, FakeMailchimp())
    status, payload = discovery_api.discover("mailchimp", "audiences", {})
    assert status == 200
    assert [item["id"] for item in payload["items"]] == ["abc123"]
    assert payload["items"][0]["members"] == 1


def test_members_discovery_needs_the_audience(mailchimp_cred):
    status, _payload = discovery_api.discover("mailchimp", "members", {})
    assert status == 400


def test_members_discovery_lists_one_audience(monkeypatch, mailchimp_cred):
    fake_transport(monkeypatch, FakeMailchimp())
    status, payload = discovery_api.discover("mailchimp", "members", {"list_id": "abc123"})
    assert status == 200
    assert [item["email"] for item in payload["items"]] == [EMAIL]


def test_health_check_pings_with_the_stored_key(monkeypatch, mailchimp_cred):
    fake_transport(monkeypatch, FakeMailchimp())
    status, payload = discovery_api.test_connection("mailchimp")
    assert status == 200
    assert payload["ok"] is True
    assert "us12" in payload["detail"]


def test_failed_ping_reports_not_ok(monkeypatch, mailchimp_cred):
    fake_transport(monkeypatch, FakeMailchimp(ping_status=401))
    status, payload = discovery_api.test_connection("mailchimp")
    assert status == 200
    assert payload["ok"] is False


def test_unknown_resource_still_404s():
    status, _payload = discovery_api.discover("mailchimp", "lists", {})
    assert status == 404


def test_missing_credential_reports_a_failed_check(monkeypatch):
    from src.dapier.connections import credentials as credentials_module

    monkeypatch.setattr(credentials_module, "get_credential",
                        lambda credential_id: (_ for _ in ()).throw(KeyError(credential_id)))
    _status, payload = discovery_api.test_connection("mailchimp")
    assert payload["ok"] is False


# --- the actions (engine runners + registry dispatch) ---


def test_find_member_found(monkeypatch, mailchimp_cred):
    from src.dapier.engine.actions import mailchimp as mailchimp_actions

    fake = fake_transport(monkeypatch, FakeMailchimp())
    output = mailchimp_actions.run_mailchimp_find_member(
        {"list_id": "abc123", "email": EMAIL}, {"data": {}})
    assert output["found"] is True
    assert output["member"]["email"] == EMAIL
    assert fake.calls[-1][1].endswith(f"/lists/abc123/members/{DIGEST}")


def test_find_member_miss_is_not_an_error(monkeypatch, mailchimp_cred):
    from src.dapier.engine.actions import mailchimp as mailchimp_actions

    fake_transport(monkeypatch, FakeMailchimp(member_exists=False))
    output = mailchimp_actions.run_mailchimp_find_member(
        {"list_id": "abc123", "email": EMAIL}, {"data": {}})
    assert output == {"found": False, "member": None}


def test_upsert_puts_the_md5_member_path(monkeypatch, mailchimp_cred):
    from src.dapier.engine.actions import mailchimp as mailchimp_actions

    fake = fake_transport(monkeypatch, FakeMailchimp())
    output = mailchimp_actions.run_mailchimp_upsert_member(
        {"list_id": "abc123", "email": EMAIL, "merge_fields": '{"FNAME": "{name}"}'},
        {"data": {"name": "Person"}})
    assert output["updated"] is True
    method, url, _headers, body = fake.calls[-1]
    assert method == "PUT"
    assert url.endswith(f"/lists/abc123/members/{DIGEST}")
    assert json.loads(body) == {"email_address": EMAIL, "status_if_new": "subscribed",
                                "merge_fields": {"FNAME": "Person"}}


def test_upsert_rejects_bad_merge_fields(monkeypatch, mailchimp_cred):
    from src.dapier.engine.actions import mailchimp as mailchimp_actions

    with pytest.raises(ValueError):
        mailchimp_actions.run_mailchimp_upsert_member(
            {"list_id": "abc123", "email": EMAIL, "merge_fields": "{oops"},
            {"data": {}})


def test_upsert_rejects_an_unknown_status(monkeypatch, mailchimp_cred):
    from src.dapier.engine.actions import mailchimp as mailchimp_actions

    with pytest.raises(ValueError):
        mailchimp_actions.run_mailchimp_upsert_member(
            {"list_id": "abc123", "email": EMAIL, "status": "maybe"}, {"data": {}})


def test_registry_dispatch_reaches_the_actions(monkeypatch, mailchimp_cred):
    from src.dapier.connectors import registry

    fake_transport(monkeypatch, FakeMailchimp())
    output = registry.run_action({"type": "mailchimp_find_member",
                                  "list_id": "abc123", "email": EMAIL},
                                 {"data": {}})
    assert output["found"] is True


def test_step_test_supports_the_new_types():
    from src.dapier.connectors import registry
    from src.dapier.engine import dryrun

    for step_type in ("mailchimp_find_member", "mailchimp_upsert_member"):
        assert step_type in registry.ACTIONS
        assert step_type in dryrun._supported_action_types()


# --- admin surface (the designer's audience picker) ---


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
    # Roles v1: the dispatcher gates through require_role now.
    monkeypatch.setattr(session, "require_role",
                        lambda event, minimum="operator": ({"sub": "op-1"}, None))
    # Roles v1: the dispatcher gates through require_role now.
    monkeypatch.setattr(session, "require_role",
                        lambda event, minimum="operator": ({"sub": "op-1"}, None))
    monkeypatch.setattr(session, "_audit_event", lambda *args, **kwargs: {"emitted": True})


def test_admin_discover_and_test_the_mailchimp_credential(monkeypatch, admin_session,
                                                          mailchimp_cred):
    fake_transport(monkeypatch, FakeMailchimp())
    catalog = admin.route(admin_request("GET", "/api/admin/connections/mailchimp/discover"),
                          "GET", "/api/admin/connections/mailchimp/discover")
    assert catalog["statusCode"] == 200
    assert {"audiences", "members"} <= {
        r["name"] for r in json.loads(catalog["body"])["resources"]}

    items = admin.route(
        admin_request("GET", "/api/admin/connections/mailchimp/discover/audiences"),
        "GET", "/api/admin/connections/mailchimp/discover/audiences")
    assert items["statusCode"] == 200
    assert [item["id"] for item in json.loads(items["body"])["items"]] == ["abc123"]

    verdict = admin.route(admin_request("POST", "/api/admin/connections/mailchimp/test", body={}),
                          "POST", "/api/admin/connections/mailchimp/test")
    assert verdict["statusCode"] == 200
    assert json.loads(verdict["body"])["ok"] is True


def test_admin_mailchimp_discover_requires_signin(monkeypatch, admin_session):
    monkeypatch.setattr(session, "authenticated", lambda event: False)
    response = admin.route(admin_request("GET", "/api/admin/connections/mailchimp/discover"),
                           "GET", "/api/admin/connections/mailchimp/discover")
    assert response["statusCode"] == 401


# --- CLI surface ---


def test_cli_routes_mailchimp_discover_and_test_through_the_agent_api(monkeypatch):
    from dapier_cli import commands, main

    calls = []
    monkeypatch.setattr(commands, "connections_discover",
                        lambda api_url, connection_id, resource=None, params=(), debug=False:
                        calls.append(("discover", connection_id, resource)) or 0)
    monkeypatch.setattr(commands, "connections_test",
                        lambda api_url, connection_id, debug=False:
                        calls.append(("test", connection_id)) or 0)
    assert main.main(["connections", "discover", "mailchimp"]) == 0
    assert main.main(["connections", "test", "mailchimp"]) == 0
    assert calls == [("discover", "mailchimp", None), ("test", "mailchimp")]
